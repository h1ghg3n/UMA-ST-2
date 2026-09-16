"""Shared fakes and fixtures for WIN5 staff Discord adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import get_ident
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from sqlalchemy import create_engine

from uma_st2.adapters.discord import (
    Win5NormalResultConfirmView,
    Win5NormalResultModal,
    Win5NormalRoundCreationDraft,
    Win5NormalRoundCreationModal,
    Win5NormalRoundCreationPreviewView,
    Win5NormalRoundEntryCorrectionModal,
    Win5NormalScoringConfirmView,
    Win5NormalScoringPreview,
    Win5NormalScoringTargetView,
    Win5RoundCreationSeasonView,
    Win5RoundLifecycleConfirmView,
    Win5RoundLifecyclePreview,
    Win5RoundLifecycleTargetView,
    Win5SeasonConfirmView,
    Win5SeasonMetadataModal,
    Win5SeasonMutationPreview,
    Win5SeasonTargetView,
    Win5SetupRoundDeletionConfirmView,
    Win5SetupRoundDeletionPreview,
    Win5SetupRoundDeletionReasonModal,
    Win5SetupRoundDeletionTargetView,
    Win5SpecialResultConfirmView,
    Win5SpecialResultEditorView,
    Win5SpecialResultModal,
    Win5SpecialResultPreview,
    Win5SpecialResultTargetView,
    Win5SpecialRoundCancellationConfirmView,
    Win5SpecialRoundCancellationPreview,
    Win5SpecialRoundCancellationReasonModal,
    Win5SpecialRoundCancellationTargetView,
    Win5SpecialRoundCreationModal,
    Win5SpecialScoringConfirmView,
    Win5SpecialScoringPreview,
    Win5SpecialScoringTargetView,
    Win5SpecialVoidConfirmView,
    Win5SpecialVoidPreview,
    Win5SpecialVoidRaceView,
    Win5SpecialVoidReasonModal,
    Win5SpecialVoidTargetView,
    Win5StaffCommandGroup,
    Win5StaffDiscordAdapter,
    Win5StaffInteractionContext,
    Win5StaffRoundActionView,
    Win5StaffSeasonActionView,
    build_normal_round_creation_draft,
    format_normal_result_preview,
    format_normal_round_creation_preview,
    format_normal_scoring_preview,
    format_round_creation_success,
    format_round_lifecycle_preview,
    format_setup_round_deletion_preview,
    format_special_result_editor,
    format_special_result_preview,
    format_special_round_cancellation_preview,
    format_special_scoring_preview,
    format_special_void_preview,
    parse_normal_result_gate_numbers,
    parse_normal_round_entries,
    parse_optional_normal_race_schedule,
    parse_special_result_gate_numbers,
    parse_special_round_race_names,
)
from uma_st2.application.win5 import (
    CancelledSpecialWin5Round,
    CancelSpecialWin5Round,
    ChangedWin5Season,
    CreatedWin5Round,
    CreateWin5Round,
    CreateWin5Season,
    DeletedWin5SetupRound,
    DeleteWin5SetupRound,
    SavedNormalWin5Result,
    SavedSpecialWin5Result,
    SaveNormalWin5Result,
    SaveSpecialWin5Result,
    ScoredNormalWin5Round,
    ScoredSpecialWin5Round,
    ScoreNormalWin5Round,
    ScoreSpecialWin5Round,
    SetSpecialWin5RaceVoid,
    TransitionedWin5Round,
    TransitionWin5Round,
    TransitionWin5Season,
    UpdatedSpecialWin5Void,
    UpdateWin5SeasonMetadata,
    Win5CreatedEntry,
    Win5CreatedRace,
    Win5NormalResultAuditType,
    Win5NormalResultEntryOption,
    Win5NormalResultPlacementInput,
    Win5NormalResultTarget,
    Win5NormalResultTargetChoice,
    Win5NormalResultTargetMode,
    Win5NormalResultVersionConflictError,
    Win5NormalScoredEventSummary,
    Win5NormalScoringWalletUnavailableError,
    Win5RoundCreationAuditType,
    Win5RoundCreationSeasonChoice,
    Win5RoundCreationSeasonPage,
    Win5RoundCreationSnapshot,
    Win5RoundCreationUnavailableError,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleAuditType,
    Win5RoundLifecycleTarget,
    Win5RoundLifecycleTargetChoice,
    Win5RoundLifecycleTargetPage,
    Win5SeasonAction,
    Win5SeasonAuditType,
    Win5SeasonRoundState,
    Win5SeasonSnapshot,
    Win5SeasonTargetPage,
    Win5SetupRoundDeletionAuditType,
    Win5SetupRoundDeletionEntry,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionTargetChoice,
    Win5SetupRoundDeletionTargetPage,
    Win5SpecialRaceVoidFact,
    Win5SpecialResultAuditType,
    Win5SpecialResultRace,
    Win5SpecialResultReferenceEntry,
    Win5SpecialResultTarget,
    Win5SpecialResultTargetChoice,
    Win5SpecialResultTargetMode,
    Win5SpecialResultVersionConflictError,
    Win5SpecialResultWinnerInput,
    Win5SpecialScoredEventSummary,
    Win5SpecialVoidAuditType,
    Win5SpecialVoidStateSnapshot,
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTarget,
    Win5StaffSpecialVoidTargetChoice,
)
from uma_st2.compose import compose_win5_command_group
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    Win5SubmissionTier,
    fingerprint_normal_result,
    fingerprint_special_result,
    fingerprint_special_void_state,
)
from uma_st2.infrastructure.database import DatabaseRuntime


def _creation_season(
    *,
    status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Win5RoundCreationSeasonChoice:
    return Win5RoundCreationSeasonChoice(
        id=7,
        name="2026 @everyone 하반기",
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
    )


def _normal_creation_draft(*, count: int = 5) -> Win5NormalRoundCreationDraft:
    return build_normal_round_creation_draft(
        season=_creation_season(),
        round_name="제3회 @everyone 아리마 기념",
        race_name="아리마 **기념**",
        race_schedule="2026-08-30 15:30",
        gate_numbers="\n".join(str(number) for number in range(1, count + 1)),
        horse_names="\n".join(f"말 {number} @everyone" for number in range(1, count + 1)),
    )


def _normal_creation_modal(
    adapter: Win5StaffDiscordAdapter,
    interaction: RecordingInteraction,
) -> Win5NormalRoundCreationModal:
    modal = Win5NormalRoundCreationModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        season=_creation_season(),
    )
    modal.round_name._value = "제3회 아리마 기념"
    modal.race_name._value = "아리마 기념"
    modal.race_schedule._value = "2026-08-30 15:30"
    modal.gate_numbers._value = "8\n1\n7\n2\n4"
    modal.horse_names._value = "라이스 샤워\n스페셜 위크\n메지로 맥퀸\n사일런스 스즈카\n토카이 테이오"
    return modal


def _layout_items(view: discord.ui.LayoutView, item_type: type[object]) -> list[object]:
    return [item for item in view.walk_children() if isinstance(item, item_type)]


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _layout_button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _entries() -> tuple[Win5NormalResultEntryOption, ...]:
    return tuple(
        Win5NormalResultEntryOption(
            id=100 + gate_number,
            gate_number=gate_number,
            name=f"말 {gate_number} @everyone" if gate_number == 1 else f"말 {gate_number}",
        )
        for gate_number in range(1, 7)
    )


def _current() -> tuple[Win5NormalResultPlacement, ...]:
    return tuple(
        Win5NormalResultPlacement(
            id=200 + position,
            position=position,
            race_entry_id=100 + position,
        )
        for position in range(1, 6)
    )


def _target(
    *,
    mode: Win5NormalResultTargetMode = Win5NormalResultTargetMode.CORRECTION,
) -> Win5NormalResultTarget:
    current = _current() if mode == Win5NormalResultTargetMode.CORRECTION else ()
    return Win5NormalResultTarget(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=11,
        round_name="제3회 @everyone 아리마 기념",
        race_id=17,
        race_name="아리마 **기념**",
        mode=mode,
        entries=_entries(),
        current_placements=current,
        result_fingerprint=fingerprint_normal_result(current) if current else None,
    )


def _choices(
    *,
    mode: Win5NormalResultTargetMode = Win5NormalResultTargetMode.ENTRY,
) -> tuple[Win5NormalResultTargetChoice, ...]:
    return (
        Win5NormalResultTargetChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=11,
            round_name="같은 제목 @everyone",
            race_id=17,
            race_name="같은 경기",
            mode=mode,
        ),
        Win5NormalResultTargetChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=12,
            round_name="같은 제목 @everyone",
            race_id=18,
            race_name="같은 경기",
            mode=mode,
        ),
    )


def _special_races(*, void_ids: tuple[int, ...] = ()) -> tuple[Win5SpecialResultRace, ...]:
    return tuple(
        Win5SpecialResultRace(
            id=race_id,
            name=f"Race {index} @everyone",
            entries=(
                Win5SpecialResultReferenceEntry(
                    id=300 + index,
                    gate_number=index,
                    name=f"참고 {index} @everyone",
                ),
            ),
            void_reason="공식 취소 @everyone" if race_id in void_ids else None,
            voided_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC) if race_id in void_ids else None,
        )
        for index, race_id in enumerate((101, 102, 103), start=1)
    )


def _special_current() -> tuple[Win5SpecialResultWinner, ...]:
    return tuple(
        Win5SpecialResultWinner(
            id=400 + index,
            race_id=race_id,
            gate_number=gate_number,
        )
        for index, (race_id, gate_number) in enumerate(
            ((101, 3), (102, 7), (103, 1)),
            start=1,
        )
    )


def _special_target(
    *,
    mode: Win5SpecialResultTargetMode = Win5SpecialResultTargetMode.CORRECTION,
    void_ids: tuple[int, ...] = (),
) -> Win5SpecialResultTarget:
    current = (
        tuple(winner for winner in _special_current() if winner.race_id not in void_ids)
        if mode == Win5SpecialResultTargetMode.CORRECTION
        else ()
    )
    return Win5SpecialResultTarget(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=21,
        round_name="여름 @everyone Special",
        mode=mode,
        races=_special_races(void_ids=void_ids),
        current_winners=current,
        result_fingerprint=fingerprint_special_result(current) if current else None,
    )


def _special_choices(
    *,
    mode: Win5SpecialResultTargetMode = Win5SpecialResultTargetMode.ENTRY,
    void_count: int = 0,
) -> tuple[Win5SpecialResultTargetChoice, ...]:
    return tuple(
        Win5SpecialResultTargetChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=21 + index,
            round_name="같은 특별 라운드 @everyone",
            race_count=3,
            mode=mode,
            void_count=void_count,
        )
        for index in range(2)
    )


def _special_void_target(
    *,
    first_void: bool = False,
    result_count: int = 0,
) -> Win5StaffSpecialVoidTarget:
    races = tuple(
        Win5StaffSpecialVoidRace(
            id=race_id,
            name=f"Race {index} @everyone",
            void_reason="공식 취소" if first_void and index == 1 else None,
            voided_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC) if first_void and index == 1 else None,
        )
        for index, race_id in enumerate((101, 102, 103), start=1)
    )
    void_ids = tuple(race.id for race in races if race.is_void)
    return Win5StaffSpecialVoidTarget(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=21,
        round_name="여름 @everyone Special",
        races=races,
        result_count=result_count,
        void_fingerprint=fingerprint_special_void_state(void_ids),
    )


def _special_void_choices() -> tuple[Win5StaffSpecialVoidTargetChoice, ...]:
    return (
        Win5StaffSpecialVoidTargetChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=21,
            round_name="같은 특별 라운드 @everyone",
            race_count=3,
            void_count=0,
        ),
        Win5StaffSpecialVoidTargetChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=22,
            round_name="같은 특별 라운드 @everyone",
            race_count=4,
            void_count=1,
        ),
    )


def _updated_special_void(*, target_voided: bool = True) -> UpdatedSpecialWin5Void:
    target = _special_void_target(first_void=target_voided)
    voids = (
        (
            Win5SpecialRaceVoidFact(
                race_id=101,
                reason="공식 취소",
                voided_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            ),
        )
        if target_voided
        else ()
    )
    return UpdatedSpecialWin5Void(
        state=Win5SpecialVoidStateSnapshot(
            season_id=7,
            round_id=21,
            round_status=Win5RoundStatus.CLOSED,
            race_ids=tuple(race.id for race in target.races),
            voids=voids,
        ),
        target_race_id=101,
        target_voided=target_voided,
        operation_type=(Win5SpecialVoidAuditType.VOIDED if target_voided else Win5SpecialVoidAuditType.RESTORED),
    )


def _cancelled_special_round() -> CancelledSpecialWin5Round:
    target = _special_void_target(first_void=True)
    voids = (
        Win5SpecialRaceVoidFact(
            race_id=101,
            reason="기존 공식 취소",
            voided_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
        ),
        Win5SpecialRaceVoidFact(
            race_id=102,
            reason="전체 행사 취소",
            voided_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        ),
        Win5SpecialRaceVoidFact(
            race_id=103,
            reason="전체 행사 취소",
            voided_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        ),
    )
    return CancelledSpecialWin5Round(
        state=Win5SpecialVoidStateSnapshot(
            season_id=7,
            round_id=21,
            round_status=Win5RoundStatus.CANCELLED,
            race_ids=tuple(race.id for race in target.races),
            voids=voids,
        ),
        newly_voided_race_ids=(102, 103),
    )


def _lifecycle_choices(
    *,
    action: Win5RoundLifecycleAction = Win5RoundLifecycleAction.OPEN,
    count: int = 2,
) -> tuple[Win5RoundLifecycleTargetChoice, ...]:
    status = Win5RoundStatus.SETUP if action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.OPEN
    return tuple(
        Win5RoundLifecycleTargetChoice(
            season_id=7,
            season_name="2026 @everyone 하반기",
            season_status=Win5SeasonStatus.ACTIVE,
            round_id=11 + index,
            round_name="같은 라운드 @everyone",
            round_type=Win5RoundType.NORMAL if index % 2 == 0 else Win5RoundType.SPECIAL,
            round_status=status,
            race_count=1 if index % 2 == 0 else 5,
            race_entry_count=5 if index % 2 == 0 else 0,
            result_count=0,
            season_open_round_count=3,
        )
        for index in range(count)
    )


def _lifecycle_target(
    *,
    action: Win5RoundLifecycleAction = Win5RoundLifecycleAction.OPEN,
) -> Win5RoundLifecycleTarget:
    return Win5RoundLifecycleTarget(
        action=action,
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=11,
        round_name="제3회 @everyone 아리마 기념",
        round_type=Win5RoundType.NORMAL,
        current_status=(Win5RoundStatus.SETUP if action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.OPEN),
        race_count=1,
        race_entry_count=6,
        accepted_submission_count=3 if action == Win5RoundLifecycleAction.CLOSE else 0,
        season_open_round_count=4,
    )


def _season_snapshot(
    *,
    status: Win5SeasonStatus = Win5SeasonStatus.DRAFT,
    rounds: Win5SeasonRoundState | None = None,
) -> Win5SeasonSnapshot:
    return Win5SeasonSnapshot(
        id=7,
        name="2026 @everyone 하반기",
        status=status,
        starts_at=datetime(2026, 8, 30, 6, 30, tzinfo=UTC),
        ends_at=None,
        rounds=rounds or Win5SeasonRoundState(),
    )


def _changed_season(
    *,
    status: Win5SeasonStatus = Win5SeasonStatus.DRAFT,
    operation_type: Win5SeasonAuditType = Win5SeasonAuditType.CREATED,
) -> ChangedWin5Season:
    return ChangedWin5Season(
        snapshot=_season_snapshot(status=status),
        operation_type=operation_type,
    )


class RecordingQueries:
    def __init__(
        self,
        *,
        choices: tuple[Win5NormalResultTargetChoice, ...] = (),
        target: Win5NormalResultTarget | None = None,
        special_choices: tuple[Win5SpecialResultTargetChoice, ...] = (),
        special_target: Win5SpecialResultTarget | None = None,
        error: Exception | None = None,
    ) -> None:
        self.choices = choices
        self.target = target
        self.special_choices = special_choices
        self.special_target = special_target
        self.error = error
        self.list_calls: list[tuple[Win5NormalResultTargetMode, int, int]] = []
        self.get_calls: list[tuple[int, Win5NormalResultTargetMode, int]] = []
        self.special_list_calls: list[tuple[Win5SpecialResultTargetMode, int, int]] = []
        self.special_get_calls: list[tuple[int, Win5SpecialResultTargetMode, int]] = []

    def list_normal_result_targets(
        self,
        *,
        mode: Win5NormalResultTargetMode,
        limit: int,
    ) -> tuple[Win5NormalResultTargetChoice, ...]:
        self.list_calls.append((mode, limit, get_ident()))
        if self.error is not None:
            raise self.error
        return self.choices

    def get_normal_result_target(
        self,
        *,
        round_id: int,
        expected_mode: Win5NormalResultTargetMode,
    ) -> Win5NormalResultTarget:
        self.get_calls.append((round_id, expected_mode, get_ident()))
        if self.error is not None:
            raise self.error
        if self.target is None:
            raise AssertionError("No target was configured for this test.")
        return self.target

    def list_special_result_targets(
        self,
        *,
        mode: Win5SpecialResultTargetMode,
        limit: int,
    ) -> tuple[Win5SpecialResultTargetChoice, ...]:
        self.special_list_calls.append((mode, limit, get_ident()))
        if self.error is not None:
            raise self.error
        return self.special_choices

    def get_special_result_target(
        self,
        *,
        round_id: int,
        expected_mode: Win5SpecialResultTargetMode,
    ) -> Win5SpecialResultTarget:
        self.special_get_calls.append((round_id, expected_mode, get_ident()))
        if self.error is not None:
            raise self.error
        if self.special_target is None:
            raise AssertionError("No Special target was configured for this test.")
        return self.special_target


class RecordingCommands:
    def __init__(
        self,
        *,
        result: SavedNormalWin5Result | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[SaveNormalWin5Result, int]] = []

    def save_result(self, command: SaveNormalWin5Result) -> SavedNormalWin5Result:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No command result was configured for this test.")
        return self.result


class RecordingSpecialCommands:
    def __init__(
        self,
        *,
        result: SavedSpecialWin5Result | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[SaveSpecialWin5Result, int]] = []

    def save_result(self, command: SaveSpecialWin5Result) -> SavedSpecialWin5Result:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No Special command result was configured for this test.")
        return self.result


class RecordingScoringCommands:
    def __init__(
        self,
        *,
        result: ScoredNormalWin5Round | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[ScoreNormalWin5Round, int]] = []

    def score_round(self, command: ScoreNormalWin5Round) -> ScoredNormalWin5Round:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No scoring result was configured for this test.")
        return self.result


class RecordingSpecialScoringCommands:
    def __init__(
        self,
        *,
        result: ScoredSpecialWin5Round | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[ScoreSpecialWin5Round, int]] = []

    def score_round(self, command: ScoreSpecialWin5Round) -> ScoredSpecialWin5Round:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No Special scoring result was configured for this test.")
        return self.result


class RecordingSpecialVoidQueries:
    def __init__(
        self,
        *,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...] = (),
        target: Win5StaffSpecialVoidTarget | None = None,
        error: Exception | None = None,
    ) -> None:
        self.choices = choices
        self.target = target
        self.error = error
        self.list_calls: list[tuple[int, int]] = []
        self.get_calls: list[tuple[int, int]] = []

    def list_targets(self, *, limit: int) -> tuple[Win5StaffSpecialVoidTargetChoice, ...]:
        self.list_calls.append((limit, get_ident()))
        if self.error is not None:
            raise self.error
        return self.choices

    def get_target(self, *, round_id: int) -> Win5StaffSpecialVoidTarget:
        self.get_calls.append((round_id, get_ident()))
        if self.error is not None:
            raise self.error
        if self.target is None:
            raise AssertionError("No Special void target was configured for this test.")
        return self.target


class RecordingSpecialVoidCommands:
    def __init__(
        self,
        *,
        result: UpdatedSpecialWin5Void | None = None,
        cancellation_result: CancelledSpecialWin5Round | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.cancellation_result = cancellation_result
        self.error = error
        self.calls: list[tuple[SetSpecialWin5RaceVoid, int]] = []
        self.cancellation_calls: list[tuple[CancelSpecialWin5Round, int]] = []

    def set_race_void(self, command: SetSpecialWin5RaceVoid) -> UpdatedSpecialWin5Void:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No Special void command result was configured for this test.")
        return self.result

    def cancel_round(self, command: CancelSpecialWin5Round) -> CancelledSpecialWin5Round:
        self.cancellation_calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.cancellation_result is None:
            raise AssertionError("No Special Round cancellation result was configured for this test.")
        return self.cancellation_result


class RecordingCreationQueries:
    def __init__(
        self,
        *,
        page: Win5RoundCreationSeasonPage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.page = page
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def list_round_creation_seasons(
        self,
        *,
        offset: int,
        limit: int,
    ) -> Win5RoundCreationSeasonPage:
        self.calls.append((offset, limit, get_ident()))
        if self.error is not None:
            raise self.error
        if self.page is not None:
            return self.page
        return Win5RoundCreationSeasonPage(
            offset=offset,
            limit=limit,
            choices=(
                Win5RoundCreationSeasonChoice(
                    id=7,
                    name="2026 @everyone 하반기",
                    status=Win5SeasonStatus.ACTIVE,
                ),
            ),
        )


class RecordingCreationCommands:
    def __init__(
        self,
        *,
        result: CreatedWin5Round | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[CreateWin5Round, int]] = []

    def create_round(self, command: CreateWin5Round) -> CreatedWin5Round:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No Round creation result was configured for this test.")
        return self.result


class RecordingLifecycleQueries:
    def __init__(
        self,
        *,
        page: Win5RoundLifecycleTargetPage | None = None,
        target: Win5RoundLifecycleTarget | None = None,
        error: Exception | None = None,
    ) -> None:
        self.page = page
        self.target = target
        self.error = error
        self.list_calls: list[tuple[Win5RoundLifecycleAction, int, int, int]] = []
        self.get_calls: list[tuple[int, Win5RoundLifecycleAction, int]] = []

    def list_round_lifecycle_targets(
        self,
        *,
        action: Win5RoundLifecycleAction,
        offset: int,
        limit: int,
    ) -> Win5RoundLifecycleTargetPage:
        self.list_calls.append((action, offset, limit, get_ident()))
        if self.error is not None:
            raise self.error
        if self.page is not None:
            return self.page
        return Win5RoundLifecycleTargetPage(
            action=action,
            offset=offset,
            limit=limit,
            choices=_lifecycle_choices(action=action),
        )

    def get_round_lifecycle_target(
        self,
        *,
        round_id: int,
        expected_action: Win5RoundLifecycleAction,
    ) -> Win5RoundLifecycleTarget:
        self.get_calls.append((round_id, expected_action, get_ident()))
        if self.error is not None:
            raise self.error
        if self.target is None:
            raise AssertionError("No lifecycle target was configured for this test.")
        return self.target


class RecordingLifecycleCommands:
    def __init__(
        self,
        *,
        result: TransitionedWin5Round | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[TransitionWin5Round, int]] = []

    def transition_round(self, command: TransitionWin5Round) -> TransitionedWin5Round:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No lifecycle result was configured for this test.")
        return self.result


class RecordingSeasonQueries:
    def __init__(
        self,
        *,
        page: Win5SeasonTargetPage | None = None,
        target: Win5SeasonSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.page = page
        self.target = target
        self.error = error
        self.list_calls: list[tuple[Win5SeasonAction, int, int, int]] = []
        self.get_calls: list[tuple[int, Win5SeasonAction, int]] = []

    def list_season_targets(
        self,
        *,
        action: Win5SeasonAction,
        offset: int,
        limit: int,
    ) -> Win5SeasonTargetPage:
        self.list_calls.append((action, offset, limit, get_ident()))
        if self.error is not None:
            raise self.error
        if self.page is not None:
            return self.page
        return Win5SeasonTargetPage(
            action=action,
            offset=offset,
            limit=limit,
            choices=(self.target or _season_snapshot(),),
        )

    def get_season_target(
        self,
        *,
        season_id: int,
        expected_action: Win5SeasonAction,
    ) -> Win5SeasonSnapshot:
        self.get_calls.append((season_id, expected_action, get_ident()))
        if self.error is not None:
            raise self.error
        if self.target is None:
            raise AssertionError("No Season target was configured for this test.")
        return self.target


class RecordingSeasonCommands:
    def __init__(
        self,
        *,
        result: ChangedWin5Season | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.create_calls: list[tuple[CreateWin5Season, int]] = []
        self.transition_calls: list[tuple[TransitionWin5Season, int]] = []
        self.update_calls: list[tuple[UpdateWin5SeasonMetadata, int]] = []

    def _result(self) -> ChangedWin5Season:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No Season command result was configured for this test.")
        return self.result

    def create_season(self, command: CreateWin5Season) -> ChangedWin5Season:
        self.create_calls.append((command, get_ident()))
        return self._result()

    def transition_season(self, command: TransitionWin5Season) -> ChangedWin5Season:
        self.transition_calls.append((command, get_ident()))
        return self._result()

    def update_metadata(self, command: UpdateWin5SeasonMetadata) -> ChangedWin5Season:
        self.update_calls.append((command, get_ident()))
        return self._result()


class RecordingDeletionQueries:
    def __init__(
        self,
        *,
        page: Win5SetupRoundDeletionTargetPage | None = None,
        target: Win5SetupRoundDeletionSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.page = page
        self.target = target
        self.error = error
        self.list_calls: list[tuple[int, int, int]] = []
        self.get_calls: list[tuple[int, int]] = []

    def list_setup_round_deletion_targets(
        self,
        *,
        offset: int,
        limit: int,
    ) -> Win5SetupRoundDeletionTargetPage:
        self.list_calls.append((offset, limit, get_ident()))
        if self.error is not None:
            raise self.error
        if self.page is not None:
            return self.page
        return Win5SetupRoundDeletionTargetPage(
            offset=offset,
            limit=limit,
            choices=(
                Win5SetupRoundDeletionTargetChoice(
                    season_id=7,
                    season_name="2026 @everyone 하반기",
                    season_status=Win5SeasonStatus.ACTIVE,
                    round_id=11,
                    round_name="삭제할 @everyone Round",
                    round_type=Win5RoundType.NORMAL,
                    round_status=Win5RoundStatus.SETUP,
                    race_count=1,
                    entry_count=5,
                ),
            ),
        )

    def get_setup_round_deletion_target(self, *, round_id: int) -> Win5SetupRoundDeletionSnapshot:
        self.get_calls.append((round_id, get_ident()))
        if self.error is not None:
            raise self.error
        if self.target is None:
            raise AssertionError("No setup Round deletion target was configured for this test.")
        return self.target


class RecordingDeletionCommands:
    def __init__(
        self,
        *,
        result: DeletedWin5SetupRound | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[DeleteWin5SetupRound, int]] = []

    def delete_round(self, command: DeleteWin5SetupRound) -> DeletedWin5SetupRound:
        self.calls.append((command, get_ident()))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("No setup Round deletion result was configured for this test.")
        return self.result


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        if self.error is not None:
            raise self.error
        return self.allowed


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


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    edits: list[dict[str, object]] = field(default_factory=list)
    modals: list[discord.ui.Modal] = field(default_factory=list)
    done: bool = False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modals.append(modal)
        self.done = True


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _run_in_test_worker[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return await asyncio.get_running_loop().run_in_executor(executor, operation)


def _saved_result(
    placements: tuple[Win5NormalResultPlacementInput, ...],
) -> SavedNormalWin5Result:
    stored = tuple(
        Win5NormalResultPlacement(
            id=200 + placement.position,
            position=placement.position,
            race_entry_id=placement.race_entry_id,
        )
        for placement in placements
    )
    return SavedNormalWin5Result(
        season_id=7,
        round_id=11,
        race_id=17,
        result_fingerprint=fingerprint_normal_result(stored),
        placements=stored,
        operation_type=Win5NormalResultAuditType.CORRECTED,
    )


def _saved_special_result(
    winners: tuple[Win5SpecialResultWinnerInput, ...],
) -> SavedSpecialWin5Result:
    stored = tuple(
        Win5SpecialResultWinner(
            id=400 + index,
            race_id=winner.race_id,
            gate_number=winner.gate_number,
        )
        for index, winner in enumerate(winners, start=1)
    )
    return SavedSpecialWin5Result(
        season_id=7,
        round_id=21,
        result_fingerprint=fingerprint_special_result(stored),
        winners=stored,
        operation_type=Win5SpecialResultAuditType.CORRECTED,
    )


def _scored_result() -> ScoredNormalWin5Round:
    return ScoredNormalWin5Round(
        season_id=7,
        round_id=11,
        race_id=17,
        result_fingerprint=fingerprint_normal_result(_current()),
        events=(
            Win5NormalScoredEventSummary(
                submission_id=31,
                submission_version=4,
                persona_id="persona-1",
                tier=Win5SubmissionTier.TOP5,
                exact_count=1,
                wrong_position_count=1,
                off_board_count=1,
                missing_count=2,
                season_score_delta=4,
                top1_score_delta=0,
                circle_point_reward=10,
            ),
        ),
    )


def _scored_special_result() -> ScoredSpecialWin5Round:
    return ScoredSpecialWin5Round(
        season_id=7,
        round_id=21,
        race_ids=(101, 102, 103),
        result_fingerprint=fingerprint_special_result(_special_current()),
        events=(
            Win5SpecialScoredEventSummary(
                submission_id=41,
                submission_version=2,
                persona_id="persona-1",
                race_count=3,
                exact_count=1,
                off_board_count=1,
                missing_count=1,
                season_score_delta=1,
                top1_score_delta=1,
            ),
        ),
    )


def _transitioned_round(
    *,
    action: Win5RoundLifecycleAction = Win5RoundLifecycleAction.OPEN,
) -> TransitionedWin5Round:
    return TransitionedWin5Round(
        season_id=7,
        season_name="2026 @everyone 하반기",
        round_id=11,
        round_name="제3회 @everyone 아리마 기념",
        round_type=Win5RoundType.NORMAL,
        status=Win5RoundStatus.OPEN if action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.CLOSED,
        operation_type=(
            Win5RoundLifecycleAuditType.OPENED
            if action == Win5RoundLifecycleAction.OPEN
            else Win5RoundLifecycleAuditType.CLOSED
        ),
    )


def _created_round(
    *,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
) -> CreatedWin5Round:
    scheduled_at = datetime(2026, 8, 30, 6, 30, tzinfo=UTC)
    races = (
        (
            Win5CreatedRace(
                id=101,
                name="아리마 @everyone 기념",
                scheduled_at=scheduled_at,
                entries=tuple(
                    Win5CreatedEntry(
                        id=200 + gate_number,
                        gate_number=gate_number,
                        name=f"말 {gate_number}",
                    )
                    for gate_number in (1, 2, 4, 7, 8)
                ),
            ),
        )
        if round_type == Win5RoundType.NORMAL
        else (
            Win5CreatedRace(id=101, name="삿포로 @everyone 1R"),
            Win5CreatedRace(id=102, name="니가타 2R"),
        )
    )
    return CreatedWin5Round(
        snapshot=Win5RoundCreationSnapshot(
            season_id=7,
            season_name="2026 @everyone 하반기",
            season_status=Win5SeasonStatus.ACTIVE,
            round_id=11,
            round_name="제3회 @everyone 아리마 기념",
            round_type=round_type,
            round_status=Win5RoundStatus.SETUP,
            races=races,
        ),
        operation_type=(
            Win5RoundCreationAuditType.NORMAL_CREATED
            if round_type == Win5RoundType.NORMAL
            else Win5RoundCreationAuditType.SPECIAL_CREATED
        ),
    )


def _deletion_snapshot() -> Win5SetupRoundDeletionSnapshot:
    return Win5SetupRoundDeletionSnapshot(
        season_id=7,
        season_name="2026 @everyone 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="삭제할 @everyone Round",
        round_type=Win5RoundType.NORMAL,
        round_status=Win5RoundStatus.SETUP,
        source_kind=Win5RoundSourceKind.NATIVE_V2,
        races=(
            Win5SetupRoundDeletionRace(
                id=101,
                name="아리마 @everyone 기념",
                entries=tuple(
                    Win5SetupRoundDeletionEntry(
                        id=200 + gate_number,
                        gate_number=gate_number,
                        name=f"말 {gate_number}",
                    )
                    for gate_number in (1, 2, 4, 7, 8)
                ),
            ),
        ),
    )


def _deleted_round() -> DeletedWin5SetupRound:
    return DeletedWin5SetupRound(
        snapshot=_deletion_snapshot(),
        operation_type=Win5SetupRoundDeletionAuditType.DELETED,
    )


def _adapter(
    queries: RecordingQueries,
    commands: RecordingCommands | None = None,
    *,
    special_commands: RecordingSpecialCommands | None = None,
    scoring_commands: RecordingScoringCommands | None = None,
    special_scoring_commands: RecordingSpecialScoringCommands | None = None,
    special_void_queries: RecordingSpecialVoidQueries | None = None,
    special_void_commands: RecordingSpecialVoidCommands | None = None,
    creation_queries: RecordingCreationQueries | None = None,
    creation_commands: RecordingCreationCommands | None = None,
    lifecycle_queries: RecordingLifecycleQueries | None = None,
    lifecycle_commands: RecordingLifecycleCommands | None = None,
    season_queries: RecordingSeasonQueries | None = None,
    season_commands: RecordingSeasonCommands | None = None,
    deletion_queries: RecordingDeletionQueries | None = None,
    deletion_commands: RecordingDeletionCommands | None = None,
    authorization: RecordingAuthorization | None = None,
    preparation: RecordingPreparation | None = None,
) -> Win5StaffDiscordAdapter:
    return Win5StaffDiscordAdapter(
        queries=queries,  # type: ignore[arg-type]
        commands=commands or RecordingCommands(),  # type: ignore[arg-type]
        special_commands=special_commands or RecordingSpecialCommands(),  # type: ignore[arg-type]
        scoring_commands=scoring_commands or RecordingScoringCommands(),  # type: ignore[arg-type]
        special_scoring_commands=special_scoring_commands or RecordingSpecialScoringCommands(),  # type: ignore[arg-type]
        special_void_queries=special_void_queries or RecordingSpecialVoidQueries(),  # type: ignore[arg-type]
        special_void_commands=special_void_commands or RecordingSpecialVoidCommands(),  # type: ignore[arg-type]
        creation_queries=creation_queries or RecordingCreationQueries(),  # type: ignore[arg-type]
        creation_commands=creation_commands or RecordingCreationCommands(),  # type: ignore[arg-type]
        lifecycle_queries=lifecycle_queries or RecordingLifecycleQueries(),  # type: ignore[arg-type]
        lifecycle_commands=lifecycle_commands or RecordingLifecycleCommands(),  # type: ignore[arg-type]
        season_queries=season_queries or RecordingSeasonQueries(),  # type: ignore[arg-type]
        season_commands=season_commands or RecordingSeasonCommands(),  # type: ignore[arg-type]
        deletion_queries=deletion_queries or RecordingDeletionQueries(),  # type: ignore[arg-type]
        deletion_commands=deletion_commands or RecordingDeletionCommands(),  # type: ignore[arg-type]
        authorize_interaction=authorization or RecordingAuthorization(),  # type: ignore[arg-type]
        prepare_command=preparation or RecordingPreparation(),  # type: ignore[arg-type]
        blocking_runner=_run_in_test_worker,
    )


__all__ = (
    "CancelSpecialWin5Round",
    "DatabaseRuntime",
    "RecordingAuthorization",
    "RecordingCommands",
    "RecordingCreationCommands",
    "RecordingCreationQueries",
    "RecordingDeletionCommands",
    "RecordingDeletionQueries",
    "RecordingInteraction",
    "RecordingLifecycleCommands",
    "RecordingLifecycleQueries",
    "RecordingPreparation",
    "RecordingQueries",
    "RecordingScoringCommands",
    "RecordingSeasonCommands",
    "RecordingSeasonQueries",
    "RecordingSpecialCommands",
    "RecordingSpecialScoringCommands",
    "RecordingSpecialVoidCommands",
    "RecordingSpecialVoidQueries",
    "SetSpecialWin5RaceVoid",
    "SimpleNamespace",
    "UTC",
    "Win5NormalResultConfirmView",
    "Win5NormalResultModal",
    "Win5NormalResultPlacementInput",
    "Win5NormalResultTargetMode",
    "Win5NormalResultVersionConflictError",
    "Win5NormalRoundCreationModal",
    "Win5NormalRoundCreationPreviewView",
    "Win5NormalRoundEntryCorrectionModal",
    "Win5NormalScoringConfirmView",
    "Win5NormalScoringPreview",
    "Win5NormalScoringTargetView",
    "Win5NormalScoringWalletUnavailableError",
    "Win5RoundCreationSeasonChoice",
    "Win5RoundCreationSeasonPage",
    "Win5RoundCreationSeasonView",
    "Win5RoundCreationUnavailableError",
    "Win5RoundLifecycleAction",
    "Win5RoundLifecycleConfirmView",
    "Win5RoundLifecyclePreview",
    "Win5RoundLifecycleTargetPage",
    "Win5RoundLifecycleTargetView",
    "Win5RoundSourceKind",
    "Win5RoundStatus",
    "Win5RoundType",
    "Win5SeasonAction",
    "Win5SeasonConfirmView",
    "Win5SeasonMetadataModal",
    "Win5SeasonMutationPreview",
    "Win5SeasonRoundState",
    "Win5SeasonStatus",
    "Win5SeasonTargetPage",
    "Win5SeasonTargetView",
    "Win5SetupRoundDeletionConfirmView",
    "Win5SetupRoundDeletionPreview",
    "Win5SetupRoundDeletionRace",
    "Win5SetupRoundDeletionReasonModal",
    "Win5SetupRoundDeletionSnapshot",
    "Win5SetupRoundDeletionTargetChoice",
    "Win5SetupRoundDeletionTargetPage",
    "Win5SetupRoundDeletionTargetView",
    "Win5SpecialResultConfirmView",
    "Win5SpecialResultEditorView",
    "Win5SpecialResultModal",
    "Win5SpecialResultPreview",
    "Win5SpecialResultRace",
    "Win5SpecialResultTarget",
    "Win5SpecialResultTargetMode",
    "Win5SpecialResultTargetView",
    "Win5SpecialResultVersionConflictError",
    "Win5SpecialResultWinnerInput",
    "Win5SpecialRoundCancellationConfirmView",
    "Win5SpecialRoundCancellationPreview",
    "Win5SpecialRoundCancellationReasonModal",
    "Win5SpecialRoundCancellationTargetView",
    "Win5SpecialRoundCreationModal",
    "Win5SpecialScoringConfirmView",
    "Win5SpecialScoringPreview",
    "Win5SpecialScoringTargetView",
    "Win5SpecialVoidConfirmView",
    "Win5SpecialVoidPreview",
    "Win5SpecialVoidRaceView",
    "Win5SpecialVoidReasonModal",
    "Win5SpecialVoidTargetView",
    "Win5StaffCommandGroup",
    "Win5StaffInteractionContext",
    "Win5StaffRoundActionView",
    "Win5StaffSeasonActionView",
    "Win5StaffSpecialVoidRace",
    "Win5StaffSpecialVoidTarget",
    "_adapter",
    "_cancelled_special_round",
    "_changed_season",
    "_choices",
    "_created_round",
    "_creation_season",
    "_deleted_round",
    "_deletion_snapshot",
    "_layout_button",
    "_layout_items",
    "_layout_text",
    "_lifecycle_choices",
    "_lifecycle_target",
    "_normal_creation_draft",
    "_normal_creation_modal",
    "_saved_result",
    "_saved_special_result",
    "_scored_result",
    "_scored_special_result",
    "_season_snapshot",
    "_special_choices",
    "_special_target",
    "_special_void_choices",
    "_special_void_target",
    "_target",
    "_transitioned_round",
    "_updated_special_void",
    "app_commands",
    "asyncio",
    "build_normal_round_creation_draft",
    "compose_win5_command_group",
    "create_engine",
    "datetime",
    "discord",
    "fingerprint_special_void_state",
    "format_normal_result_preview",
    "format_normal_round_creation_preview",
    "format_normal_scoring_preview",
    "format_round_creation_success",
    "format_round_lifecycle_preview",
    "format_setup_round_deletion_preview",
    "format_special_result_editor",
    "format_special_result_preview",
    "format_special_round_cancellation_preview",
    "format_special_scoring_preview",
    "format_special_void_preview",
    "get_ident",
    "parse_normal_result_gate_numbers",
    "parse_normal_round_entries",
    "parse_optional_normal_race_schedule",
    "parse_special_result_gate_numbers",
    "parse_special_round_race_names",
    "pytest",
)
