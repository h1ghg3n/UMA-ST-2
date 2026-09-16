"""Discord adapter for bounded staff WIN5 Round interactions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime

import discord
from discord import app_commands

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
    Win5NormalResultCommandError,
    Win5NormalResultCommands,
    Win5NormalResultPlacementInput,
    Win5NormalScoringCommands,
    Win5NormalScoringError,
    Win5RoundCreationCommands,
    Win5RoundCreationEntryInput,
    Win5RoundCreationError,
    Win5RoundCreationRaceInput,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleCommands,
    Win5RoundLifecycleError,
    Win5SeasonAction,
    Win5SeasonLifecycleCommands,
    Win5SeasonLifecycleError,
    Win5SeasonSnapshot,
    Win5SetupRoundDeletionCommands,
    Win5SetupRoundDeletionError,
    Win5SetupRoundDeletionSnapshot,
    Win5SpecialResultCommandError,
    Win5SpecialResultCommands,
    Win5SpecialResultWinnerInput,
    Win5SpecialScoringCommands,
    Win5SpecialScoringError,
    Win5SpecialVoidCommands,
    Win5SpecialVoidError,
)
from uma_st2.application.win5.staff_result_queries import (
    Win5NormalResultTarget,
    Win5NormalResultTargetChoice,
    Win5NormalResultTargetMode,
    Win5SpecialResultRace,
    Win5SpecialResultTarget,
    Win5SpecialResultTargetChoice,
    Win5SpecialResultTargetMode,
    Win5StaffResultQueries,
    Win5StaffResultQueryError,
)
from uma_st2.application.win5.staff_round_creation_queries import (
    Win5RoundCreationSeasonChoice,
    Win5RoundCreationSeasonPage,
    Win5StaffRoundCreationQueries,
    Win5StaffRoundCreationQueryError,
)
from uma_st2.application.win5.staff_round_deletion_queries import (
    Win5SetupRoundDeletionTargetChoice,
    Win5SetupRoundDeletionTargetPage,
    Win5StaffRoundDeletionQueries,
    Win5StaffRoundDeletionQueryError,
)
from uma_st2.application.win5.staff_round_lifecycle_queries import (
    Win5RoundLifecycleTarget,
    Win5RoundLifecycleTargetChoice,
    Win5RoundLifecycleTargetPage,
    Win5StaffRoundLifecycleQueries,
    Win5StaffRoundLifecycleQueryError,
)
from uma_st2.application.win5.staff_season_queries import (
    Win5SeasonTargetPage,
    Win5StaffSeasonQueries,
    Win5StaffSeasonQueryError,
)
from uma_st2.application.win5.staff_special_void_queries import (
    Win5StaffSpecialVoidQueries,
    Win5StaffSpecialVoidQueryError,
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTarget,
    Win5StaffSpecialVoidTargetChoice,
)
from uma_st2.domain.win5 import MIN_NORMAL_WIN5_RACE_ENTRIES, Win5RoundType

from .common import (
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
)
from .datetime_codec import format_discord_datetime, parse_operator_date_and_time
from .strings import win5_registration as registration_copy
from .strings import win5_staff_common as common_copy
from .strings import win5_staff_creation as creation_copy
from .strings import win5_staff_deletion as deletion_copy
from .strings import win5_staff_lifecycle as lifecycle_copy
from .strings import win5_staff_results as results_copy
from .strings import win5_staff_scoring as scoring_copy
from .strings import win5_staff_season as season_copy
from .strings import win5_staff_void as void_copy

_COMMAND_NAME = "win5.staff.round"
_SEASON_COMMAND_NAME = "win5.staff.season"
_COMPONENT_TIMEOUT_SECONDS = 600
_NORMAL_ROUND_CREATE_ACTION = "create"
_SPECIAL_ROUND_CREATE_ACTION = "special-create"
_SPECIAL_RESULT_ENTRY_ACTION = "special-result-entry"
_SPECIAL_RESULT_CORRECTION_ACTION = "special-result-correction"
_NORMAL_SCORING_ACTION = "normal-scoring"
_SPECIAL_SCORING_ACTION = "special-scoring"
_SPECIAL_VOID_ACTION = "special-void"
_SPECIAL_ROUND_CANCEL_ACTION = "special-round-cancel"
_ROUND_OPEN_ACTION = "round-open"
_ROUND_CLOSE_ACTION = "round-close"
_ROUND_DELETE_ACTION = "round-delete"
_LIFECYCLE_PAGE_SIZE = 25
_CREATION_SEASON_PAGE_SIZE = 25
_NORMAL_CREATION_ENTRY_PAGE_SIZE = 20
_DELETION_PAGE_SIZE = 25
_SPECIAL_VOID_RACE_PAGE_SIZE = 25
_SEASON_PAGE_SIZE = 25

logger = logging.getLogger(__name__)


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Discord interaction has no valid {field_name}.")
    return value


@dataclass(frozen=True, slots=True)
class Win5StaffInteractionContext:
    """User/guild/channel binding captured by the opening interaction."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> Win5StaffInteractionContext:
        return cls(
            user_id=_required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            ),
            guild_id=_required_snowflake(
                getattr(interaction, "guild_id", None),
                field_name="guild ID",
            ),
            channel_id=_required_snowflake(
                getattr(interaction, "channel_id", None),
                field_name="channel ID",
            ),
        )

    def matches(self, interaction: object) -> bool:
        return (
            getattr(getattr(interaction, "user", None), "id", None),
            getattr(interaction, "guild_id", None),
            getattr(interaction, "channel_id", None),
        ) == (self.user_id, self.guild_id, self.channel_id)


def parse_normal_result_gate_numbers(value: str) -> tuple[int, ...]:
    """Parse exactly five distinct positive gate numbers from adapter text."""

    if not isinstance(value, str) or re.fullmatch(r"\s*\d+\s*(?:[-,]\s*\d+\s*){4}", value) is None:
        raise ValueError(results_copy.NORMAL_GATE_INPUT_FORMAT)
    numbers = tuple(int(token) for token in re.findall(r"\d+", value))
    if any(number <= 0 for number in numbers) or len(set(numbers)) != 5:
        raise ValueError(results_copy.NORMAL_GATE_DISTINCT)
    return numbers


def parse_special_result_gate_numbers(value: str, *, expected_count: int) -> tuple[int, ...]:
    """Parse one positive winner gate per displayed Special Race line."""

    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise ValueError("expected_count must be a positive integer.")
    if not isinstance(value, str):
        raise ValueError(results_copy.SPECIAL_GATE_LINES)
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    if len(lines) != expected_count or any(re.fullmatch(r"\d+", line) is None for line in lines):
        raise ValueError(results_copy.special_gate_count(expected_count))
    numbers = tuple(int(line) for line in lines)
    if any(number <= 0 for number in numbers):
        raise ValueError(results_copy.SPECIAL_GATE_POSITIVE)
    return numbers


def parse_special_round_race_names(value: str) -> tuple[str, ...]:
    """Parse ordered non-empty Special Race names from multiline operator input."""

    if not isinstance(value, str):
        raise ValueError(creation_copy.SPECIAL_RACE_NAME_LINES)
    names = tuple(line.strip() for line in value.splitlines() if line.strip())
    if not names:
        raise ValueError(creation_copy.SPECIAL_RACE_REQUIRED)
    if any(len(name) > 200 for name in names):
        raise ValueError(creation_copy.SPECIAL_RACE_NAME_LENGTH)
    return names


def parse_normal_round_entries(
    gate_numbers_value: str,
    horse_names_value: str,
) -> tuple[Win5RoundCreationEntryInput, ...]:
    """Pair non-empty gate/name lines by their shared input index."""

    if not isinstance(gate_numbers_value, str) or not isinstance(horse_names_value, str):
        raise ValueError(creation_copy.ENTRY_LIST_REQUIRED)
    gate_lines = tuple(line.strip() for line in gate_numbers_value.splitlines() if line.strip())
    horse_names = tuple(line.strip() for line in horse_names_value.splitlines() if line.strip())
    if len(gate_lines) != len(horse_names):
        raise ValueError(creation_copy.ENTRY_COUNT_MISMATCH)
    if not gate_lines:
        raise ValueError(creation_copy.ENTRY_LINES_REQUIRED)
    if any(re.fullmatch(r"\d+", line) is None for line in gate_lines):
        raise ValueError(creation_copy.GATE_LINE_FORMAT)
    gate_numbers = tuple(int(line) for line in gate_lines)
    if any(gate_number <= 0 for gate_number in gate_numbers):
        raise ValueError(creation_copy.GATE_POSITIVE)
    if len(set(gate_numbers)) != len(gate_numbers):
        raise ValueError(creation_copy.DUPLICATE_GATE)
    try:
        entries = tuple(
            Win5RoundCreationEntryInput(gate_number=gate_number, name=horse_name)
            for gate_number, horse_name in zip(gate_numbers, horse_names, strict=True)
        )
    except ValueError as error:
        raise ValueError(creation_copy.HORSE_NAME_LINES) from error
    return tuple(sorted(entries, key=lambda entry: entry.gate_number))


@dataclass(frozen=True, slots=True)
class Win5NormalRoundCreationDraftEntry:
    """One adapter-local editable row; ``row_id`` is never persisted."""

    row_id: int
    gate_number: int
    horse_name: str

    def __post_init__(self) -> None:
        if isinstance(self.row_id, bool) or not isinstance(self.row_id, int) or self.row_id <= 0:
            raise ValueError("draft row_id must be a positive integer.")
        canonical = Win5RoundCreationEntryInput(
            gate_number=self.gate_number,
            name=self.horse_name,
        )
        object.__setattr__(self, "gate_number", canonical.gate_number)
        object.__setattr__(self, "horse_name", canonical.name)

    def to_application_input(self) -> Win5RoundCreationEntryInput:
        return Win5RoundCreationEntryInput(
            gate_number=self.gate_number,
            name=self.horse_name,
        )


@dataclass(frozen=True, slots=True)
class Win5NormalRoundCreationDraft:
    """Complete zero-write Normal creation state carried only by Discord UI."""

    season: Win5RoundCreationSeasonChoice
    round_name: str
    race_name: str
    scheduled_at: datetime | None
    entries: tuple[Win5NormalRoundCreationDraftEntry, ...]
    page: int = 0
    reviewed_pages: frozenset[int] = frozenset({0})

    def __post_init__(self) -> None:
        if not isinstance(self.season, Win5RoundCreationSeasonChoice):
            raise ValueError("season must be a Win5RoundCreationSeasonChoice.")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, Win5NormalRoundCreationDraftEntry) for entry in self.entries
        ):
            raise ValueError("entries must be a tuple of adapter-local draft rows.")
        if len({entry.row_id for entry in self.entries}) != len(self.entries):
            raise ValueError("draft row IDs must be unique.")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 0:
            raise ValueError("draft page must be a non-negative integer.")
        if not isinstance(self.reviewed_pages, frozenset) or any(
            isinstance(page, bool) or not isinstance(page, int) or page < 0 for page in self.reviewed_pages
        ):
            raise ValueError("reviewed_pages must contain non-negative integers.")

        # Reuse the existing Application payload boundary for all canonical
        # Normal invariants. Constructing this DTO has no UoW or write effect.
        validation = self.to_command(
            idempotency_key="adapter-local-normal-round-draft",
            actor_discord_user_id="adapter-local-draft",
            guild_id=None,
            correlation_id=None,
        )
        race = validation.races[0]
        object.__setattr__(self, "round_name", validation.round_name)
        object.__setattr__(self, "race_name", race.name)
        object.__setattr__(self, "scheduled_at", race.scheduled_at)
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda entry: (entry.gate_number, entry.row_id))),
        )

    def to_command(
        self,
        *,
        idempotency_key: str,
        actor_discord_user_id: str,
        guild_id: str | None,
        correlation_id: str | None,
    ) -> CreateWin5Round:
        return CreateWin5Round(
            season_id=self.season.id,
            round_type=Win5RoundType.NORMAL,
            round_name=self.round_name,
            races=(
                Win5RoundCreationRaceInput(
                    name=self.race_name,
                    scheduled_at=self.scheduled_at,
                    entries=tuple(entry.to_application_input() for entry in self.entries),
                ),
            ),
            idempotency_key=idempotency_key,
            actor_discord_user_id=actor_discord_user_id,
            guild_id=guild_id,
            correlation_id=correlation_id,
        )

    def corrected(
        self,
        *,
        row_id: int,
        gate_number: int,
        horse_name: str,
    ) -> Win5NormalRoundCreationDraft:
        if not any(entry.row_id == row_id for entry in self.entries):
            raise ValueError(creation_copy.DRAFT_ENTRY_MISSING)
        replacement = Win5NormalRoundCreationDraftEntry(
            row_id=row_id,
            gate_number=gate_number,
            horse_name=horse_name,
        )
        updated = replace(
            self,
            entries=tuple(replacement if entry.row_id == row_id else entry for entry in self.entries),
        )
        index = next(index for index, entry in enumerate(updated.entries) if entry.row_id == row_id)
        page = index // _NORMAL_CREATION_ENTRY_PAGE_SIZE
        return replace(
            updated,
            page=page,
            reviewed_pages=updated.reviewed_pages | {page},
        )

    def viewed(self, page: int) -> Win5NormalRoundCreationDraft:
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < _normal_creation_page_count(self):
            raise ValueError(creation_copy.DRAFT_PAGE_INVALID)
        return replace(self, page=page, reviewed_pages=self.reviewed_pages | {page})

    @property
    def all_pages_reviewed(self) -> bool:
        return frozenset(range(_normal_creation_page_count(self))).issubset(self.reviewed_pages)


def build_normal_round_creation_draft(
    *,
    season: Win5RoundCreationSeasonChoice,
    round_name: str,
    race_name: str,
    race_schedule: str,
    gate_numbers: str,
    horse_names: str,
) -> Win5NormalRoundCreationDraft:
    """Build one complete adapter-local draft without opening a UoW."""

    entries = parse_normal_round_entries(gate_numbers, horse_names)
    try:
        return Win5NormalRoundCreationDraft(
            season=season,
            round_name=round_name,
            race_name=race_name,
            scheduled_at=parse_optional_normal_race_schedule(race_schedule),
            entries=tuple(
                Win5NormalRoundCreationDraftEntry(
                    row_id=index,
                    gate_number=entry.gate_number,
                    horse_name=entry.name,
                )
                for index, entry in enumerate(entries, start=1)
            ),
        )
    except ValueError as error:
        if "at least" in str(error):
            raise ValueError(creation_copy.minimum_entry_count(MIN_NORMAL_WIN5_RACE_ENTRIES)) from error
        if "gate numbers must be unique" in str(error):
            raise ValueError(creation_copy.DUPLICATE_GATE) from error
        raise


def _normal_creation_page_count(draft: Win5NormalRoundCreationDraft) -> int:
    return max(1, (len(draft.entries) + _NORMAL_CREATION_ENTRY_PAGE_SIZE - 1) // _NORMAL_CREATION_ENTRY_PAGE_SIZE)


def _normal_creation_page_entries(
    draft: Win5NormalRoundCreationDraft,
) -> tuple[Win5NormalRoundCreationDraftEntry, ...]:
    start = draft.page * _NORMAL_CREATION_ENTRY_PAGE_SIZE
    return draft.entries[start : start + _NORMAL_CREATION_ENTRY_PAGE_SIZE]


def _round_creation_season_warnings(
    season: Win5RoundCreationSeasonChoice,
) -> tuple[str, ...]:
    return creation_copy.season_warnings(season)


def _round_creation_season_period(season: Win5RoundCreationSeasonChoice) -> str:
    return creation_copy.season_period(season)


def format_normal_round_creation_preview(
    draft: Win5NormalRoundCreationDraft,
    *,
    notice: str | None = None,
) -> str:
    """Render one complete, explicitly paged view of the adapter-local draft."""

    page_count = _normal_creation_page_count(draft)
    start = draft.page * _NORMAL_CREATION_ENTRY_PAGE_SIZE
    visible = _normal_creation_page_entries(draft)
    end = start + len(visible)
    return creation_copy.format_normal_round_creation_preview_copy(
        season=draft.season,
        round_name=draft.round_name,
        race_name=draft.race_name,
        scheduled_at=draft.scheduled_at,
        entries=tuple((entry.gate_number, entry.horse_name) for entry in draft.entries),
        page=draft.page,
        page_count=page_count,
        start=start,
        end=end,
        notice=notice,
    )


def _normal_creation_terminal_layout(message: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=_COMPONENT_TIMEOUT_SECONDS)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(bounded_discord_message((message,), limit=3500))))
    return view


def parse_optional_normal_race_schedule(value: str) -> datetime | None:
    """Parse one optional combined KST date/time field into aware UTC."""

    if not isinstance(value, str):
        raise ValueError(creation_copy.RACE_SCHEDULE_FORMAT)
    text = value.strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        raise ValueError(creation_copy.RACE_SCHEDULE_FORMAT)
    try:
        return parse_operator_date_and_time(parts[0], parts[1])
    except ValueError as exc:
        raise ValueError(creation_copy.RACE_SCHEDULE_VALID) from exc


def _target_select_options(
    choices: tuple[Win5NormalResultTargetChoice, ...],
) -> list[discord.SelectOption]:
    return results_copy.normal_target_options(choices)


def _special_target_select_options(
    choices: tuple[Win5SpecialResultTargetChoice, ...],
) -> list[discord.SelectOption]:
    return results_copy.special_target_options(choices)


def _special_void_target_select_options(
    choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
) -> list[discord.SelectOption]:
    return void_copy.target_options(choices)


def _special_void_race_page_count(target: Win5StaffSpecialVoidTarget) -> int:
    return max(1, (len(target.races) + _SPECIAL_VOID_RACE_PAGE_SIZE - 1) // _SPECIAL_VOID_RACE_PAGE_SIZE)


def _special_void_race_page(
    target: Win5StaffSpecialVoidTarget,
    *,
    page: int,
) -> tuple[Win5StaffSpecialVoidRace, ...]:
    page_count = _special_void_race_page_count(target)
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < page_count:
        raise ValueError(void_copy.RACE_PAGE_INVALID)
    start = page * _SPECIAL_VOID_RACE_PAGE_SIZE
    return target.races[start : start + _SPECIAL_VOID_RACE_PAGE_SIZE]


@dataclass(frozen=True, slots=True)
class Win5SpecialVoidPreview:
    """Fresh zero-write Special Race-void state carried into confirmation."""

    target: Win5StaffSpecialVoidTarget
    race_id: int
    target_voided: bool
    reason: str

    def __post_init__(self) -> None:
        race = next((item for item in self.target.races if item.id == self.race_id), None)
        if race is None:
            raise ValueError(void_copy.RACE_NOT_IN_ROUND)
        if not isinstance(self.target_voided, bool) or self.target_voided == race.is_void:
            raise ValueError(void_copy.VOID_DIRECTION_INVALID)
        reason = self.reason.strip()
        if not reason or len(reason) > 255:
            raise ValueError(void_copy.REASON_LENGTH)
        object.__setattr__(self, "reason", reason)

    @property
    def race(self) -> Win5StaffSpecialVoidRace:
        return next(item for item in self.target.races if item.id == self.race_id)


def format_special_void_preview(preview: Win5SpecialVoidPreview) -> str:
    """Render the complete operational consequence before changing void facts."""

    return void_copy.format_preview(
        target=preview.target,
        race=preview.race,
        target_voided=preview.target_voided,
        reason=preview.reason,
    )


def format_special_void_success(
    preview: Win5SpecialVoidPreview,
    result: UpdatedSpecialWin5Void,
) -> str:
    """Render a private acknowledgement from committed Special void facts."""

    return void_copy.format_success(
        target=preview.target,
        race=preview.race,
        result=result,
    )


def _special_void_command_error_message(error: Win5SpecialVoidError) -> str:
    return void_copy.command_error_message(error)


@dataclass(frozen=True, slots=True)
class Win5SpecialRoundCancellationPreview:
    """Fresh complete state carried into one atomic all-void confirmation."""

    target: Win5StaffSpecialVoidTarget
    reason: str

    def __post_init__(self) -> None:
        reason = self.reason.strip()
        if not reason or len(reason) > 255:
            raise ValueError(void_copy.WHOLE_REASON_LENGTH)
        object.__setattr__(self, "reason", reason)

    @property
    def newly_voided_count(self) -> int:
        return sum(not race.is_void for race in self.target.races)


def format_special_round_cancellation_preview(preview: Win5SpecialRoundCancellationPreview) -> str:
    """Render one complete, irreversible whole-Round cancellation consequence."""

    return void_copy.format_whole_preview(
        target=preview.target,
        reason=preview.reason,
        newly_voided_count=preview.newly_voided_count,
    )


def format_special_round_cancellation_success(
    preview: Win5SpecialRoundCancellationPreview,
    result: CancelledSpecialWin5Round,
) -> str:
    """Render a private receipt from the committed all-void state."""

    return void_copy.format_whole_success(target=preview.target, result=result)


def _special_round_cancellation_error_message(error: Win5SpecialVoidError) -> str:
    return void_copy.whole_command_error_message(error)


@dataclass(frozen=True, slots=True)
class Win5NormalResultPreview:
    """Bounded primitive/DTO state carried from Modal preview to confirmation."""

    target: Win5NormalResultTarget
    placements: tuple[Win5NormalResultPlacementInput, ...]
    reason: str | None


def format_normal_result_preview(preview: Win5NormalResultPreview) -> str:
    """Render a mention-safe before/after preview for authoritative confirmation."""

    return results_copy.format_normal_preview(
        target=preview.target,
        placements=preview.placements,
        reason=preview.reason,
    )


def format_normal_result_success(
    preview: Win5NormalResultPreview,
    result: SavedNormalWin5Result,
) -> str:
    """Render a private acknowledgement from the committed result DTO."""

    return results_copy.format_normal_success(target=preview.target, result=result)


def _result_command_error_message(error: Win5NormalResultCommandError) -> str:
    return results_copy.normal_command_error_message(error)


@dataclass(frozen=True, slots=True)
class Win5SpecialResultPreview:
    """Complete Special winner bundle carried from Modal preview to confirmation."""

    target: Win5SpecialResultTarget
    winners: tuple[Win5SpecialResultWinnerInput, ...]
    reason: str | None


def _special_void_label(race: Win5SpecialResultRace) -> str:
    return results_copy.special_void_label(race)


def _special_gate_label(race: Win5SpecialResultRace, gate_number: int) -> str:
    return results_copy.special_gate_label(race, gate_number)


def format_special_result_editor(target: Win5SpecialResultTarget) -> str:
    """Render the full current Race/void mapping before opening the input Modal."""

    return results_copy.format_special_editor(target)


def format_special_result_preview(preview: Win5SpecialResultPreview) -> str:
    """Render every Special Race winner or reject an unsafe truncated preview."""

    return results_copy.format_special_preview(
        target=preview.target,
        winners=preview.winners,
        reason=preview.reason,
    )


def format_special_result_success(
    preview: Win5SpecialResultPreview,
    result: SavedSpecialWin5Result,
) -> str:
    """Render a private acknowledgement from the committed Special result DTO."""

    return results_copy.format_special_success(target=preview.target, result=result)


def _special_result_command_error_message(error: Win5SpecialResultCommandError) -> str:
    return results_copy.special_command_error_message(error)


@dataclass(frozen=True, slots=True)
class Win5NormalScoringPreview:
    """Current authoritative result facts shown before Normal scoring."""

    target: Win5NormalResultTarget


def format_normal_scoring_preview(preview: Win5NormalScoringPreview) -> str:
    """Render a mention-safe confirmation from the current complete result board."""

    return scoring_copy.format_normal_preview(preview.target)


def format_normal_scoring_success(
    preview: Win5NormalScoringPreview,
    result: ScoredNormalWin5Round,
) -> str:
    """Render a private acknowledgement from the committed scoring result."""

    return scoring_copy.format_normal_success(target=preview.target, result=result)


def _scoring_command_error_message(error: Win5NormalScoringError) -> str:
    return scoring_copy.normal_command_error_message(error)


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringPreview:
    """Complete authoritative Special winner bundle shown before scoring."""

    target: Win5SpecialResultTarget


def format_special_scoring_preview(preview: Win5SpecialScoringPreview) -> str:
    """Render every Special Race winner or void fact before scoring."""

    return scoring_copy.format_special_preview(preview.target)


def format_special_scoring_success(
    preview: Win5SpecialScoringPreview,
    result: ScoredSpecialWin5Round,
) -> str:
    """Render a private acknowledgement from committed Special score facts."""

    return scoring_copy.format_special_success(target=preview.target, result=result)


def _special_scoring_command_error_message(error: Win5SpecialScoringError) -> str:
    return scoring_copy.special_command_error_message(error)


def _round_type_label(round_type: Win5RoundType) -> str:
    return creation_copy.round_type_label(round_type)


def _lifecycle_target_options(
    choices: tuple[Win5RoundLifecycleTargetChoice, ...],
) -> list[discord.SelectOption]:
    return lifecycle_copy.target_options(choices)


@dataclass(frozen=True, slots=True)
class Win5RoundLifecyclePreview:
    """Current Round facts carried from a fresh query into confirmation."""

    target: Win5RoundLifecycleTarget


def format_round_lifecycle_preview(preview: Win5RoundLifecyclePreview) -> str:
    """Render a mention-safe Round open/close confirmation."""

    return lifecycle_copy.format_preview(preview.target)


def format_round_lifecycle_success(
    preview: Win5RoundLifecyclePreview,
    result: TransitionedWin5Round,
) -> str:
    """Render a private acknowledgement from the committed lifecycle DTO."""

    return lifecycle_copy.format_success(target=preview.target, result=result)


def _lifecycle_query_error_message(error: Win5StaffRoundLifecycleQueryError) -> str:
    return lifecycle_copy.query_error_message(error)


def _lifecycle_command_error_message(error: Win5RoundLifecycleError) -> str:
    return lifecycle_copy.command_error_message(error)


def _deletion_target_options(
    choices: tuple[Win5SetupRoundDeletionTargetChoice, ...],
) -> list[discord.SelectOption]:
    """Build bounded destructive selector options with duplicate disambiguation."""

    return deletion_copy.target_options(choices)


@dataclass(frozen=True, slots=True)
class Win5SetupRoundDeletionPreview:
    """Exact graph and required operator reason carried into confirmation."""

    snapshot: Win5SetupRoundDeletionSnapshot
    reason: str


def format_setup_round_deletion_preview(preview: Win5SetupRoundDeletionPreview) -> str:
    """Render an explicit destructive preview without implying auto-recreation."""

    return deletion_copy.format_preview(snapshot=preview.snapshot, reason=preview.reason)


def format_setup_round_deletion_success(result: DeletedWin5SetupRound) -> str:
    """Render a private acknowledgement from the retained deletion snapshot."""

    return deletion_copy.format_success(result)


def _deletion_query_error_message(error: Win5StaffRoundDeletionQueryError) -> str:
    return deletion_copy.query_error_message(error)


def _deletion_command_error_message(error: Win5SetupRoundDeletionError) -> str:
    return deletion_copy.command_error_message(error)


def _creation_season_options(
    choices: tuple[Win5RoundCreationSeasonChoice, ...],
) -> list[discord.SelectOption]:
    return creation_copy.creation_season_options(choices)


def _round_creation_season_selector_message(
    *,
    round_type: Win5RoundType,
    page: Win5RoundCreationSeasonPage,
) -> str:
    return creation_copy.season_selector_message(round_type=round_type, page=page)


def format_round_creation_success(result: CreatedWin5Round) -> str:
    """Render a private acknowledgement from the committed creation DTO."""

    return creation_copy.format_round_creation_success(result)


def _creation_command_error_message(error: Win5RoundCreationError) -> str:
    return creation_copy.creation_command_error_message(error)


def _parse_optional_kst_datetime(value: str, *, field_label: str) -> datetime | None:
    canonical = value.strip()
    if not canonical:
        return None
    parts = canonical.split()
    if len(parts) != 2:
        raise ValueError(season_copy.datetime_format_error(field_label=field_label))
    try:
        return parse_operator_date_and_time(parts[0], parts[1])
    except ValueError as error:
        raise ValueError(season_copy.datetime_value_error(field_label=field_label)) from error


def _season_datetime_label(value: datetime | None) -> str:
    return season_copy.datetime_label(value)


def _season_action_label(action: Win5SeasonAction) -> str:
    return season_copy.action_label(action)


def _season_target_options(choices: tuple[Win5SeasonSnapshot, ...]) -> list[discord.SelectOption]:
    return season_copy.target_options(choices)


@dataclass(frozen=True, slots=True)
class Win5SeasonMutationPreview:
    """Adapter-local desired input or fresh transition target carried to Confirm."""

    action: Win5SeasonAction
    target: Win5SeasonSnapshot | None = None
    desired_name: str | None = None
    desired_starts_at: datetime | None = None
    desired_ends_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", Win5SeasonAction(self.action))
        if self.action == Win5SeasonAction.CREATE:
            if self.target is not None:
                raise ValueError("Season create Preview cannot own an existing target.")
        elif self.target is None:
            raise ValueError("Existing Season action requires a target snapshot.")
        if self.action in {Win5SeasonAction.CREATE, Win5SeasonAction.EDIT}:
            self.to_command(
                idempotency_key="adapter-local-season-preview",
                actor_discord_user_id="adapter-local-preview",
                guild_id=None,
                correlation_id=None,
            )
        elif any(value is not None for value in (self.desired_name, self.desired_starts_at, self.desired_ends_at)):
            raise ValueError("Season transition Preview cannot replace metadata.")

    def to_command(
        self,
        *,
        idempotency_key: str,
        actor_discord_user_id: str,
        guild_id: str | None,
        correlation_id: str | None,
    ) -> CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata:
        if self.action == Win5SeasonAction.CREATE:
            return CreateWin5Season(
                name=self.desired_name,  # type: ignore[arg-type]
                starts_at=self.desired_starts_at,
                ends_at=self.desired_ends_at,
                idempotency_key=idempotency_key,
                actor_discord_user_id=actor_discord_user_id,
                guild_id=guild_id,
                correlation_id=correlation_id,
                reason=self.reason,
            )
        if self.action == Win5SeasonAction.EDIT:
            return UpdateWin5SeasonMetadata(
                season_id=self.target.id,  # type: ignore[union-attr]
                name=self.desired_name,  # type: ignore[arg-type]
                starts_at=self.desired_starts_at,
                ends_at=self.desired_ends_at,
                idempotency_key=idempotency_key,
                actor_discord_user_id=actor_discord_user_id,
                guild_id=guild_id,
                correlation_id=correlation_id,
                reason=self.reason,
            )
        return TransitionWin5Season(
            season_id=self.target.id,  # type: ignore[union-attr]
            action=self.action,
            idempotency_key=idempotency_key,
            actor_discord_user_id=actor_discord_user_id,
            guild_id=guild_id,
            correlation_id=correlation_id,
            reason=self.reason,
        )


def format_season_preview(preview: Win5SeasonMutationPreview) -> str:
    """Render one mention-safe Season mutation confirmation."""

    return season_copy.format_preview_copy(
        action=preview.action,
        target=preview.target,
        desired_name=preview.desired_name,
        desired_starts_at=preview.desired_starts_at,
        desired_ends_at=preview.desired_ends_at,
        reason=preview.reason,
    )


def format_season_success(result: ChangedWin5Season) -> str:
    """Render a private receipt only from the committed/exact-retry DTO."""

    return season_copy.format_success(result)


def _season_query_error_message(error: Win5StaffSeasonQueryError) -> str:
    return season_copy.query_error_message(error)


def _season_command_error_message(error: Win5SeasonLifecycleError) -> str:
    return season_copy.command_error_message(error)


class Win5StaffSeasonActionSelect(discord.ui.Select):
    """Top-level operator actions for the independent Season panel."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
    ) -> None:
        self._adapter = adapter
        self._context = context
        super().__init__(
            custom_id="win5-staff-season-action",
            placeholder=season_copy.ACTION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=season_copy.action_options(
                create_value=Win5SeasonAction.CREATE.value,
                activate_value=Win5SeasonAction.ACTIVATE.value,
                close_value=Win5SeasonAction.CLOSE.value,
                cancel_value=Win5SeasonAction.CANCEL.value,
                edit_value=Win5SeasonAction.EDIT.value,
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            action = Win5SeasonAction(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                season_copy.ACTION_SELECTION_INVALID,
            )
            return
        if action == Win5SeasonAction.CREATE:
            await self._adapter.open_season_metadata_modal(
                interaction,
                context=self._context,
                action=action,
                target=None,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        await self._adapter.show_season_targets(
            interaction,
            context=self._context,
            action=action,
            offset=0,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5StaffSeasonActionView(discord.ui.View):
    """Ephemeral Season management action panel."""

    def __init__(self, *, adapter: Win5StaffDiscordAdapter, context: Win5StaffInteractionContext) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(Win5StaffSeasonActionSelect(adapter=adapter, context=context))


class Win5SeasonMetadataModal(discord.ui.Modal):
    """Create/edit metadata input that produces only an adapter-local Preview."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        target: Win5SeasonSnapshot | None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if action not in {Win5SeasonAction.CREATE, Win5SeasonAction.EDIT}:
            raise ValueError("Season metadata Modal supports only create or edit.")
        super().__init__(title=_season_action_label(action), timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._action = action
        self._target = target
        self._source_view = source_view
        self.name = discord.ui.TextInput(
            custom_id="win5-season-name",
            label=season_copy.SEASON_TITLE_LABEL,
            placeholder=season_copy.OPERATOR_TITLE_PLACEHOLDER,
            default=None if target is None else target.name,
            required=True,
            max_length=100,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="win5-season-starts-at",
            label=season_copy.STARTS_AT_LABEL,
            placeholder=season_copy.DATETIME_PLACEHOLDER,
            default=(
                None
                if target is None or target.starts_at is None
                else format_discord_datetime(target.starts_at).removesuffix(" KST")
            ),
            required=False,
            max_length=16,
        )
        self.ends_at = discord.ui.TextInput(
            custom_id="win5-season-ends-at",
            label=season_copy.ENDS_AT_LABEL,
            placeholder=season_copy.DATETIME_PLACEHOLDER,
            default=(
                None
                if target is None or target.ends_at is None
                else format_discord_datetime(target.ends_at).removesuffix(" KST")
            ),
            required=False,
            max_length=16,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-season-reason",
            label=season_copy.OPERATOR_MEMO_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(self.name)
        self.add_item(self.starts_at)
        self.add_item(self.ends_at)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_season_metadata(
            interaction,
            context=self._context,
            action=self._action,
            target=self._target,
            name=str(self.name.value),
            starts_at=str(self.starts_at.value),
            ends_at=str(self.ends_at.value),
            reason=str(self.reason.value).strip() or None,
            source_view=self._source_view,
        )


class Win5SeasonTargetSelect(discord.ui.Select):
    """Select one eligible Season from a bounded current page."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5SeasonTargetPage,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._action = page.action
        self._targets_by_id = {target.id: target for target in page.choices}
        super().__init__(
            custom_id=f"win5-season-{page.action.value}-target",
            placeholder=season_copy.target_placeholder(page.action),
            min_values=1,
            max_values=1,
            options=_season_target_options(page.choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            season_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                season_copy.TARGET_SELECTION_INVALID,
            )
            return
        target = self._targets_by_id.get(season_id)
        if target is None:
            await self._adapter.send_component_error(
                interaction,
                season_copy.TARGET_NOT_ON_PAGE,
            )
            return
        if self._action == Win5SeasonAction.EDIT:
            await self._adapter.open_season_metadata_modal(
                interaction,
                context=self._context,
                action=self._action,
                target=target,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        await self._adapter.preview_season_transition(
            interaction,
            context=self._context,
            action=self._action,
            season_id=season_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SeasonPageButton(discord.ui.Button):
    """Navigate Season target pages through a fresh query UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        offset: int,
        forward: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._action = action
        self._offset = offset
        super().__init__(
            label=season_copy.NEXT_LABEL if forward else season_copy.PREVIOUS_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"win5-season-{action.value}-{'next' if forward else 'previous'}-{offset}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_season_targets(
            interaction,
            context=self._context,
            action=self._action,
            offset=self._offset,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SeasonTargetView(discord.ui.View):
    """Paged Season selector without a retained Session or lock."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5SeasonTargetPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(Win5SeasonTargetSelect(adapter=adapter, context=context, page=page))
        if page.has_previous:
            self.add_item(
                Win5SeasonPageButton(
                    adapter=adapter,
                    context=context,
                    action=page.action,
                    offset=max(0, page.offset - page.limit),
                    forward=False,
                )
            )
        if page.has_next:
            self.add_item(
                Win5SeasonPageButton(
                    adapter=adapter,
                    context=context,
                    action=page.action,
                    offset=page.offset + page.limit,
                    forward=True,
                )
            )


class Win5SeasonConfirmView(discord.ui.View):
    """Final bound confirmation for one Season mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SeasonMutationPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this Preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=season_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-season-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_season_mutation(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=season_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-season-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_season_mutation(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5StaffRoundActionSelect(discord.ui.Select):
    """Incremental staff Round panel containing implemented actions."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
    ) -> None:
        self._adapter = adapter
        self._context = context
        super().__init__(
            custom_id="win5-staff-round-action",
            placeholder=common_copy.ROUND_ACTION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=common_copy.round_action_options(
                normal_create=_NORMAL_ROUND_CREATE_ACTION,
                special_create=_SPECIAL_ROUND_CREATE_ACTION,
                delete=_ROUND_DELETE_ACTION,
                open_=_ROUND_OPEN_ACTION,
                close=_ROUND_CLOSE_ACTION,
                normal_result_entry=Win5NormalResultTargetMode.ENTRY.value,
                normal_result_correction=Win5NormalResultTargetMode.CORRECTION.value,
                special_result_entry=_SPECIAL_RESULT_ENTRY_ACTION,
                special_result_correction=_SPECIAL_RESULT_CORRECTION_ACTION,
                special_void=_SPECIAL_VOID_ACTION,
                special_round_cancel=_SPECIAL_ROUND_CANCEL_ACTION,
                normal_scoring=_NORMAL_SCORING_ACTION,
                special_scoring=_SPECIAL_SCORING_ACTION,
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] in {_NORMAL_ROUND_CREATE_ACTION, _SPECIAL_ROUND_CREATE_ACTION}:
            await self._adapter.show_round_creation_seasons(
                interaction,
                context=self._context,
                round_type=(
                    Win5RoundType.NORMAL if self.values[0] == _NORMAL_ROUND_CREATE_ACTION else Win5RoundType.SPECIAL
                ),
                offset=0,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] == _ROUND_DELETE_ACTION:
            await self._adapter.show_setup_round_deletion_targets(
                interaction,
                context=self._context,
                offset=0,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] in {_ROUND_OPEN_ACTION, _ROUND_CLOSE_ACTION}:
            await self._adapter.show_round_lifecycle_targets(
                interaction,
                context=self._context,
                action=(
                    Win5RoundLifecycleAction.OPEN
                    if self.values[0] == _ROUND_OPEN_ACTION
                    else Win5RoundLifecycleAction.CLOSE
                ),
                offset=0,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] == _NORMAL_SCORING_ACTION:
            await self._adapter.show_normal_scoring_targets(
                interaction,
                context=self._context,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] == _SPECIAL_SCORING_ACTION:
            await self._adapter.show_special_scoring_targets(
                interaction,
                context=self._context,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] == _SPECIAL_VOID_ACTION:
            await self._adapter.show_special_void_targets(
                interaction,
                context=self._context,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] == _SPECIAL_ROUND_CANCEL_ACTION:
            await self._adapter.show_special_round_cancellation_targets(
                interaction,
                context=self._context,
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        if self.values[0] in {_SPECIAL_RESULT_ENTRY_ACTION, _SPECIAL_RESULT_CORRECTION_ACTION}:
            await self._adapter.show_special_result_targets(
                interaction,
                context=self._context,
                mode=(
                    Win5SpecialResultTargetMode.ENTRY
                    if self.values[0] == _SPECIAL_RESULT_ENTRY_ACTION
                    else Win5SpecialResultTargetMode.CORRECTION
                ),
                source_view=self.view if isinstance(self.view, discord.ui.View) else None,
            )
            return
        await self._adapter.show_normal_result_targets(
            interaction,
            context=self._context,
            mode=Win5NormalResultTargetMode(self.values[0]),
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5StaffRoundActionView(discord.ui.View):
    """Ephemeral staff Round panel for implemented lifecycle/result/scoring actions."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(Win5StaffRoundActionSelect(adapter=adapter, context=context))


class Win5RoundCreationSeasonSelect(discord.ui.Select):
    """Select one eligible Season before opening a TextInput-only creation Modal."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_type: Win5RoundType,
        page: Win5RoundCreationSeasonPage,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._round_type = round_type
        self._seasons_by_id = {choice.id: choice for choice in page.choices}
        super().__init__(
            custom_id=f"win5-{round_type.value}-round-create-season",
            placeholder=creation_copy.SEASON_SELECT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_creation_season_options(page.choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            season_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                creation_copy.SEASON_SELECTION_INVALID,
            )
            return
        season = self._seasons_by_id.get(season_id)
        if season is None:
            await self._adapter.send_component_error(
                interaction,
                creation_copy.SEASON_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_round_creation_modal(
            interaction,
            context=self._context,
            season=season,
            round_type=self._round_type,
        )


class Win5RoundCreationSeasonPageButton(discord.ui.Button):
    """Navigate eligible Season pages through a fresh query UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_type: Win5RoundType,
        offset: int,
        forward: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._round_type = round_type
        self._offset = offset
        super().__init__(
            label=creation_copy.NEXT_LABEL if forward else creation_copy.PREVIOUS_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=(f"win5-{round_type.value}-round-create-{'next' if forward else 'previous'}-{offset}"),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_round_creation_seasons(
            interaction,
            context=self._context,
            round_type=self._round_type,
            offset=self._offset,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5RoundCreationSeasonView(discord.ui.View):
    """Paged eligible-Season selector without a retained Session or lock."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_type: Win5RoundType,
        page: Win5RoundCreationSeasonPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5RoundCreationSeasonSelect(
                adapter=adapter,
                context=context,
                round_type=round_type,
                page=page,
            )
        )
        if page.has_previous:
            self.add_item(
                Win5RoundCreationSeasonPageButton(
                    adapter=adapter,
                    context=context,
                    round_type=round_type,
                    offset=max(0, page.offset - page.limit),
                    forward=False,
                )
            )
        if page.has_next:
            self.add_item(
                Win5RoundCreationSeasonPageButton(
                    adapter=adapter,
                    context=context,
                    round_type=round_type,
                    offset=page.offset + page.limit,
                    forward=True,
                )
            )


class Win5NormalRoundCreationModal(discord.ui.Modal):
    """Bulk-input form that produces only an adapter-local Normal draft."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        season: Win5RoundCreationSeasonChoice,
        draft: Win5NormalRoundCreationDraft | None = None,
        source_view: Win5NormalRoundCreationPreviewView | None = None,
    ) -> None:
        super().__init__(
            title=creation_copy.normal_modal_title(has_warnings=bool(_round_creation_season_warnings(season))),
            timeout=_COMPONENT_TIMEOUT_SECONDS,
        )
        self._adapter = adapter
        self._context = context
        self._season = season
        self._draft = draft
        self._source_view = source_view
        ordered_entries = draft.entries if draft is not None else ()
        schedule_default = None
        if draft is not None and draft.scheduled_at is not None:
            schedule_default = format_discord_datetime(draft.scheduled_at).removesuffix(" KST")
        self.round_name = discord.ui.TextInput(
            custom_id="win5-normal-round-name",
            label=creation_copy.ROUND_TITLE_LABEL,
            placeholder=creation_copy.OPERATOR_TITLE_PLACEHOLDER,
            default=draft.round_name if draft is not None else None,
            required=True,
            max_length=100,
        )
        self.race_name = discord.ui.TextInput(
            custom_id="win5-normal-race-name",
            label=creation_copy.RACE_NAME_LABEL,
            default=draft.race_name if draft is not None else None,
            required=True,
            max_length=200,
        )
        self.race_schedule = discord.ui.TextInput(
            custom_id="win5-normal-race-schedule",
            label=creation_copy.RACE_SCHEDULE_LABEL,
            placeholder=creation_copy.DATETIME_PLACEHOLDER,
            default=schedule_default,
            required=False,
            max_length=16,
        )
        self.gate_numbers = discord.ui.TextInput(
            custom_id="win5-normal-gate-numbers",
            label=creation_copy.GATE_NUMBERS_LABEL,
            placeholder=creation_copy.GATE_NUMBERS_PLACEHOLDER,
            default="\n".join(str(entry.gate_number) for entry in ordered_entries) or None,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.horse_names = discord.ui.TextInput(
            custom_id="win5-normal-horse-names",
            label=creation_copy.HORSE_NAMES_LABEL,
            placeholder=creation_copy.HORSE_NAMES_PLACEHOLDER,
            default="\n".join(entry.horse_name for entry in ordered_entries) or None,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.round_name)
        self.add_item(self.race_name)
        self.add_item(self.race_schedule)
        self.add_item(self.gate_numbers)
        self.add_item(self.horse_names)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_normal_round_creation(
            interaction,
            context=self._context,
            season=self._season,
            round_name=str(self.round_name.value),
            race_name=str(self.race_name.value),
            race_schedule=str(self.race_schedule.value),
            gate_numbers=str(self.gate_numbers.value),
            horse_names=str(self.horse_names.value),
            previous_draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalRoundEntryCorrectionModal(discord.ui.Modal):
    """Prefilled correction for one adapter-local Normal Entry row."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        entry: Win5NormalRoundCreationDraftEntry,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        super().__init__(title=creation_copy.ENTRY_CORRECTION_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._entry = entry
        self._source_view = source_view
        self.gate_number = discord.ui.TextInput(
            custom_id="win5-normal-entry-correction-gate",
            label=creation_copy.GATE_NUMBER_LABEL,
            default=str(entry.gate_number),
            required=True,
            max_length=4000,
        )
        self.horse_name = discord.ui.TextInput(
            custom_id="win5-normal-entry-correction-name",
            label=creation_copy.HORSE_NAME_LABEL,
            default=entry.horse_name,
            required=True,
            max_length=100,
        )
        self.add_item(self.gate_number)
        self.add_item(self.horse_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.apply_normal_round_entry_correction(
            interaction,
            context=self._context,
            draft=self._draft,
            row_id=self._entry.row_id,
            gate_number=str(self.gate_number.value),
            horse_name=str(self.horse_name.value),
            source_view=self._source_view,
        )


class Win5NormalRoundEntryCorrectionSelect(discord.ui.Select):
    """Select one visible draft row for a prefilled correction Modal."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        visible = _normal_creation_page_entries(draft)
        self._entries_by_row_id = {entry.row_id: entry for entry in visible}
        super().__init__(
            custom_id="win5-normal-round-create-entry-correction",
            placeholder=creation_copy.ENTRY_CORRECTION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=creation_copy.entry_choice_label(
                        gate_number=entry.gate_number,
                        horse_name=entry.horse_name,
                    ),
                    value=str(entry.row_id),
                )
                for entry in visible
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            row_id = int(self.values[0])
            entry = self._entries_by_row_id[row_id]
        except (IndexError, KeyError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                creation_copy.ENTRY_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_normal_round_entry_correction_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            entry=entry,
            source_view=self._source_view,
        )


class Win5NormalRoundCreationPageButton(discord.ui.Button):
    """Move across the complete adapter-local Entry draft."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        page: int,
        label: str,
        source_view: Win5NormalRoundCreationPreviewView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._page = page
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"win5-normal-round-create-page-{label}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_normal_round_creation_page(
            interaction,
            context=self._context,
            draft=self._draft.viewed(self._page),
            source_view=self._source_view,
        )


class Win5NormalRoundCreationReentryButton(discord.ui.Button):
    """Open the complete bulk Modal with the current draft prefilled."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=creation_copy.REENTRY_LABEL,
            custom_id="win5-normal-round-create-reentry",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_normal_round_reentry_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalRoundCreationConfirmButton(discord.ui.Button):
    """Execute the existing command only from the final interaction."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.success,
            label=creation_copy.CONFIRM_LABEL,
            custom_id="win5-normal-round-create-confirm",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_normal_round_creation(
            interaction,
            context=self._context,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalRoundCreationCancelButton(discord.ui.Button):
    """Discard the adapter-local draft without opening a UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=creation_copy.CANCEL_LABEL,
            custom_id="win5-normal-round-create-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_normal_round_creation(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class Win5NormalRoundCreationPreviewView(discord.ui.LayoutView):
    """Complete paged preview backed only by immutable adapter-local data."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        page_count = _normal_creation_page_count(draft)
        page = min(draft.page, page_count - 1)
        if page != draft.page:
            draft = replace(draft, page=page)
        self.draft = draft
        self.notice = notice
        self._confirmation_started = False

        container = discord.ui.Container(
            discord.ui.TextDisplay(format_normal_round_creation_preview(draft, notice=notice))
        )
        correction_row = discord.ui.ActionRow()
        correction_row.add_item(
            Win5NormalRoundEntryCorrectionSelect(
                adapter=adapter,
                context=context,
                draft=draft,
                source_view=self,
            )
        )
        container.add_item(correction_row)

        if page_count > 1:
            page_row = discord.ui.ActionRow()
            page_row.add_item(
                Win5NormalRoundCreationPageButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    page=max(0, draft.page - 1),
                    label=creation_copy.PREVIOUS_LABEL,
                    source_view=self,
                    disabled=draft.page == 0,
                )
            )
            page_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=creation_copy.page_label(page=draft.page, page_count=page_count),
                    disabled=True,
                )
            )
            page_row.add_item(
                Win5NormalRoundCreationPageButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    page=min(page_count - 1, draft.page + 1),
                    label=creation_copy.NEXT_LABEL,
                    source_view=self,
                    disabled=draft.page == page_count - 1,
                )
            )
            container.add_item(page_row)

        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5NormalRoundCreationReentryButton(
                adapter=adapter,
                context=context,
                draft=draft,
                source_view=self,
            )
        )
        action_row.add_item(
            Win5NormalRoundCreationConfirmButton(
                adapter=adapter,
                context=context,
                draft=draft,
                source_view=self,
                disabled=not draft.all_pages_reviewed,
            )
        )
        action_row.add_item(
            Win5NormalRoundCreationCancelButton(
                adapter=adapter,
                context=context,
                source_view=self,
            )
        )
        container.add_item(action_row)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


class Win5SpecialRoundCreationModal(discord.ui.Modal):
    """TextInput-only Special Round creation form bound to one Season."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        season: Win5RoundCreationSeasonChoice,
    ) -> None:
        super().__init__(
            title=creation_copy.special_modal_title(has_warnings=bool(_round_creation_season_warnings(season))),
            timeout=_COMPONENT_TIMEOUT_SECONDS,
        )
        self._adapter = adapter
        self._context = context
        self._season_id = season.id
        self.round_name = discord.ui.TextInput(
            custom_id="win5-special-round-name",
            label=creation_copy.ROUND_TITLE_LABEL,
            placeholder=creation_copy.OPERATOR_TITLE_PLACEHOLDER,
            required=True,
            max_length=100,
        )
        self.race_names = discord.ui.TextInput(
            custom_id="win5-special-race-names",
            label=creation_copy.SPECIAL_RACE_NAMES_LABEL,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-round-reason",
            label=creation_copy.OPERATOR_MEMO_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(self.round_name)
        self.add_item(self.race_names)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.create_special_round(
            interaction,
            context=self._context,
            season_id=self._season_id,
            round_name=str(self.round_name.value),
            race_names=str(self.race_names.value),
            reason=str(self.reason.value).strip() or None,
        )


class Win5RoundLifecycleTargetSelect(discord.ui.Select):
    """Select one Round from a bounded lifecycle target page."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5RoundLifecycleTargetPage,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._action = page.action
        self._allowed_round_ids = frozenset(choice.round_id for choice in page.choices)
        super().__init__(
            custom_id=f"win5-round-{page.action.value}-target",
            placeholder=lifecycle_copy.target_placeholder(page.action),
            min_values=1,
            max_values=1,
            options=_lifecycle_target_options(page.choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                lifecycle_copy.TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                lifecycle_copy.TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.preview_round_lifecycle(
            interaction,
            context=self._context,
            action=self._action,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5RoundLifecyclePageButton(discord.ui.Button):
    """Navigate lifecycle selector pages through a fresh query UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        action: Win5RoundLifecycleAction,
        offset: int,
        forward: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._action = action
        self._offset = offset
        super().__init__(
            label=lifecycle_copy.NEXT_LABEL if forward else lifecycle_copy.PREVIOUS_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"win5-round-{action.value}-{'next' if forward else 'previous'}-{offset}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_round_lifecycle_targets(
            interaction,
            context=self._context,
            action=self._action,
            offset=self._offset,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5RoundLifecycleTargetView(discord.ui.View):
    """Paged lifecycle target selector without a retained Session or lock."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5RoundLifecycleTargetPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(Win5RoundLifecycleTargetSelect(adapter=adapter, context=context, page=page))
        if page.has_previous:
            self.add_item(
                Win5RoundLifecyclePageButton(
                    adapter=adapter,
                    context=context,
                    action=page.action,
                    offset=max(0, page.offset - page.limit),
                    forward=False,
                )
            )
        if page.has_next:
            self.add_item(
                Win5RoundLifecyclePageButton(
                    adapter=adapter,
                    context=context,
                    action=page.action,
                    offset=page.offset + page.limit,
                    forward=True,
                )
            )


class Win5RoundLifecycleConfirmView(discord.ui.View):
    """Final bound confirmation for one Round lifecycle mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5RoundLifecyclePreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this Preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=lifecycle_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-round-lifecycle-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_round_lifecycle(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=lifecycle_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-round-lifecycle-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_round_lifecycle(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5SetupRoundDeletionTargetSelect(discord.ui.Select):
    """Select one currently eligible setup Round before collecting a reason."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5SetupRoundDeletionTargetPage,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._allowed_round_ids = frozenset(choice.round_id for choice in page.choices)
        super().__init__(
            custom_id="win5-setup-round-delete-target",
            placeholder=deletion_copy.TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_deletion_target_options(page.choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                deletion_copy.TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                deletion_copy.TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_setup_round_deletion_reason_modal(
            interaction,
            context=self._context,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SetupRoundDeletionPageButton(discord.ui.Button):
    """Navigate setup-Round deletion pages through a fresh query UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        offset: int,
        forward: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._offset = offset
        super().__init__(
            label=deletion_copy.NEXT_LABEL if forward else deletion_copy.PREVIOUS_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"win5-setup-round-delete-{'next' if forward else 'previous'}-{offset}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_setup_round_deletion_targets(
            interaction,
            context=self._context,
            offset=self._offset,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SetupRoundDeletionTargetView(discord.ui.View):
    """Paged deletion selector without a retained Session or lock."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        page: Win5SetupRoundDeletionTargetPage,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(Win5SetupRoundDeletionTargetSelect(adapter=adapter, context=context, page=page))
        if page.has_previous:
            self.add_item(
                Win5SetupRoundDeletionPageButton(
                    adapter=adapter,
                    context=context,
                    offset=max(0, page.offset - page.limit),
                    forward=False,
                )
            )
        if page.has_next:
            self.add_item(
                Win5SetupRoundDeletionPageButton(
                    adapter=adapter,
                    context=context,
                    offset=page.offset + page.limit,
                    forward=True,
                )
            )


class Win5SetupRoundDeletionReasonModal(discord.ui.Modal):
    """Required operator reason collected before the destructive preview."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        super().__init__(title=deletion_copy.MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._round_id = round_id
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            custom_id="win5-setup-round-delete-reason",
            label=deletion_copy.REASON_LABEL,
            placeholder=deletion_copy.REASON_PLACEHOLDER,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_setup_round_deletion(
            interaction,
            context=self._context,
            round_id=self._round_id,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class Win5SetupRoundDeletionConfirmView(discord.ui.View):
    """Final bound confirmation for one irreversible setup graph deletion."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SetupRoundDeletionPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this destructive preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=deletion_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-setup-round-delete-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_setup_round_deletion(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=deletion_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-setup-round-delete-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_setup_round_deletion(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5NormalResultTargetSelect(discord.ui.Select):
    """Bounded target selection before opening the TextInput-only Modal."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        choices: tuple[Win5NormalResultTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._mode = mode
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id=f"win5-normal-result-{mode.value}-target",
            placeholder=results_copy.NORMAL_TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_target_select_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                results_copy.NORMAL_TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                results_copy.NORMAL_TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_normal_result_modal(
            interaction,
            context=self._context,
            mode=self._mode,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5NormalResultTargetView(discord.ui.View):
    """Ephemeral target selector for one result lane."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        choices: tuple[Win5NormalResultTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5NormalResultTargetSelect(
                adapter=adapter,
                context=context,
                mode=mode,
                choices=choices,
            )
        )


class Win5NormalResultModal(discord.ui.Modal):
    """TextInput-only Normal result form bound to one selected Round."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        super().__init__(title=results_copy.normal_modal_title(mode), timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._mode = mode
        self._round_id = round_id
        self._source_view = source_view
        self.result_order = discord.ui.TextInput(
            custom_id="win5-normal-result-order",
            label=results_copy.NORMAL_ORDER_LABEL,
            placeholder=results_copy.NORMAL_ORDER_PLACEHOLDER,
            required=True,
            min_length=9,
            max_length=100,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-normal-result-reason",
            label=results_copy.OPERATOR_MEMO_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(self.result_order)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_normal_result(
            interaction,
            context=self._context,
            mode=self._mode,
            round_id=self._round_id,
            result_order=str(self.result_order.value),
            reason=str(self.reason.value).strip() or None,
            source_view=self._source_view,
        )


class Win5NormalResultConfirmView(discord.ui.View):
    """Final bound confirmation for one authoritative result mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5NormalResultPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this result preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=results_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-normal-result-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_normal_result(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=results_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-normal-result-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_normal_result(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5SpecialVoidTargetSelect(discord.ui.Select):
    """Select one mutable closed Special Round."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id="win5-special-void-target",
            placeholder=void_copy.TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_special_void_target_select_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                void_copy.TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                void_copy.ROUND_NOT_ON_PAGE,
            )
            return
        await self._adapter.show_special_void_races(
            interaction,
            context=self._context,
            round_id=round_id,
            page=0,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialVoidTargetView(discord.ui.View):
    """Ephemeral Special Round selector without persistence ownership."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5SpecialVoidTargetSelect(
                adapter=adapter,
                context=context,
                choices=choices,
            )
        )


class Win5SpecialVoidRaceSelect(discord.ui.Select):
    """Select one Race from a bounded page of a complete query DTO."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        target: Win5StaffSpecialVoidTarget,
        page: int,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._target = target
        races = _special_void_race_page(target, page=page)
        self._allowed_race_ids = frozenset(race.id for race in races)
        options = void_copy.race_options(races)
        super().__init__(
            custom_id=f"win5-special-void-race-{page}",
            placeholder=void_copy.RACE_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            race_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                void_copy.RACE_SELECTION_INVALID,
            )
            return
        if race_id not in self._allowed_race_ids:
            await self._adapter.send_component_error(
                interaction,
                void_copy.RACE_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_special_void_reason_modal(
            interaction,
            context=self._context,
            target=self._target,
            race_id=race_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialVoidRacePageButton(discord.ui.Button):
    """Navigate Race pages through a fresh query UoW."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_id: int,
        page: int,
        forward: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._round_id = round_id
        self._page = page
        super().__init__(
            label=void_copy.NEXT_LABEL if forward else void_copy.PREVIOUS_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"win5-special-void-race-{'next' if forward else 'previous'}-{page}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_special_void_races(
            interaction,
            context=self._context,
            round_id=self._round_id,
            page=self._page,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialVoidRaceView(discord.ui.View):
    """Paged Race selector retaining only an immutable Application DTO."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        target: Win5StaffSpecialVoidTarget,
        page: int,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        page_count = _special_void_race_page_count(target)
        _special_void_race_page(target, page=page)
        self.add_item(
            Win5SpecialVoidRaceSelect(
                adapter=adapter,
                context=context,
                target=target,
                page=page,
            )
        )
        if page > 0:
            self.add_item(
                Win5SpecialVoidRacePageButton(
                    adapter=adapter,
                    context=context,
                    round_id=target.round_id,
                    page=page - 1,
                    forward=False,
                )
            )
        if page + 1 < page_count:
            self.add_item(
                Win5SpecialVoidRacePageButton(
                    adapter=adapter,
                    context=context,
                    round_id=target.round_id,
                    page=page + 1,
                    forward=True,
                )
            )


class Win5SpecialVoidReasonModal(discord.ui.Modal):
    """Collect the required audit reason for one selected Race transition."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        target: Win5StaffSpecialVoidTarget,
        race_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        race = next((item for item in target.races if item.id == race_id), None)
        if race is None:
            raise ValueError(void_copy.RACE_NOT_IN_ROUND)
        super().__init__(
            title=void_copy.reason_modal_title(race_is_void=race.is_void), timeout=_COMPONENT_TIMEOUT_SECONDS
        )
        self._adapter = adapter
        self._context = context
        self._target = target
        self._race_id = race_id
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-void-reason",
            label=void_copy.reason_label(race_is_void=race.is_void),
            placeholder=void_copy.RACE_REASON_PLACEHOLDER,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_special_void_change(
            interaction,
            context=self._context,
            selected_target=self._target,
            race_id=self._race_id,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class Win5SpecialVoidConfirmView(discord.ui.View):
    """Final bound confirmation for one Special Race void transition."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialVoidPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=void_copy.CHANGE_CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-special-void-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_special_void_change(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=void_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-special-void-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_special_void_change(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5SpecialRoundCancellationTargetSelect(discord.ui.Select):
    """Select one closed Special Round for atomic all-void cancellation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id="win5-special-round-cancellation-target",
            placeholder=void_copy.WHOLE_TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_special_void_target_select_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                void_copy.WHOLE_TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                void_copy.ROUND_NOT_ON_PAGE,
            )
            return
        await self._adapter.open_special_round_cancellation_reason_modal(
            interaction,
            context=self._context,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialRoundCancellationTargetView(discord.ui.View):
    """Ephemeral whole-Round cancellation target selector."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5SpecialRoundCancellationTargetSelect(
                adapter=adapter,
                context=context,
                choices=choices,
            )
        )


class Win5SpecialRoundCancellationReasonModal(discord.ui.Modal):
    """Collect one shared reason for every newly voided Race."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        super().__init__(title=void_copy.WHOLE_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._round_id = round_id
        self._source_view = source_view
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-round-cancellation-reason",
            label=void_copy.WHOLE_REASON_LABEL,
            placeholder=void_copy.WHOLE_REASON_PLACEHOLDER,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=255,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_special_round_cancellation(
            interaction,
            context=self._context,
            round_id=self._round_id,
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class Win5SpecialRoundCancellationConfirmView(discord.ui.View):
    """Final bound confirmation for one atomic all-void mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialRoundCancellationPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=void_copy.WHOLE_CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-special-round-cancellation-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_special_round_cancellation(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=void_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-special-round-cancellation-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_special_round_cancellation(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5SpecialResultTargetSelect(discord.ui.Select):
    """Select one complete Special Round before opening its result Modal."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        mode: Win5SpecialResultTargetMode,
        choices: tuple[Win5SpecialResultTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._mode = mode
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id=f"win5-special-result-{mode.value}-target",
            placeholder=results_copy.SPECIAL_TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_special_target_select_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                results_copy.SPECIAL_TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                results_copy.SPECIAL_TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.show_special_result_editor(
            interaction,
            context=self._context,
            mode=self._mode,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialResultTargetView(discord.ui.View):
    """Ephemeral target selector for one Special result lane."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        mode: Win5SpecialResultTargetMode,
        choices: tuple[Win5SpecialResultTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5SpecialResultTargetSelect(
                adapter=adapter,
                context=context,
                mode=mode,
                choices=choices,
            )
        )


class Win5SpecialResultEditorView(discord.ui.View):
    """Current Special Race/void state shown before the winner input Modal."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        target: Win5SpecialResultTarget,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._target = target

    @discord.ui.button(
        label=results_copy.SPECIAL_OPEN_MODAL_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id="win5-special-result-open-modal",
    )
    async def open_modal(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.open_special_result_modal(
            interaction,
            context=self._context,
            target=self._target,
            source_view=self,
        )


class Win5SpecialResultModal(discord.ui.Modal):
    """TextInput-only complete Special winner form bound to one Round."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        target: Win5SpecialResultTarget,
        source_view: discord.ui.View | None = None,
    ) -> None:
        super().__init__(title=results_copy.special_modal_title(target.mode), timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._target = target
        self._source_view = source_view
        current_by_race = {winner.race_id: winner for winner in target.current_winners}
        default = "\n".join(
            str(current_by_race[race.id].gate_number) for race in target.non_void_races if race.id in current_by_race
        )
        self.winner_gates = discord.ui.TextInput(
            custom_id="win5-special-result-gates",
            label=results_copy.special_gate_label_text(len(target.non_void_races)),
            placeholder=results_copy.SPECIAL_GATE_PLACEHOLDER,
            default=default or None,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-result-reason",
            label=results_copy.OPERATOR_MEMO_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(self.winner_gates)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.preview_special_result(
            interaction,
            context=self._context,
            mode=self._target.mode,
            round_id=self._target.round_id,
            winner_gates=str(self.winner_gates.value),
            reason=str(self.reason.value).strip() or None,
            source_view=self._source_view,
        )


class Win5SpecialResultConfirmView(discord.ui.View):
    """Final bound confirmation for one complete Special result mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialResultPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this Special result preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=results_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-special-result-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_special_result(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=results_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-special-result-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_special_result(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5NormalScoringTargetSelect(discord.ui.Select):
    """Bounded complete-result target selection before scoring preview."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5NormalResultTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id="win5-normal-scoring-target",
            placeholder=scoring_copy.NORMAL_TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=scoring_copy.normal_target_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                scoring_copy.NORMAL_TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                scoring_copy.NORMAL_TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.preview_normal_scoring(
            interaction,
            context=self._context,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5NormalScoringTargetView(discord.ui.View):
    """Ephemeral target selector for Normal scoring."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5NormalResultTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5NormalScoringTargetSelect(
                adapter=adapter,
                context=context,
                choices=choices,
            )
        )


class Win5NormalScoringConfirmView(discord.ui.View):
    """Final bound confirmation for one atomic Normal scoring mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5NormalScoringPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this Normal scoring preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=scoring_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-normal-scoring-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_normal_scoring(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=scoring_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-normal-scoring-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_normal_scoring(
            interaction,
            context=self._context,
            source_view=self,
        )


class Win5SpecialScoringTargetSelect(discord.ui.Select):
    """Select one complete Special Result bundle before scoring preview."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5SpecialResultTargetChoice, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._allowed_round_ids = frozenset(choice.round_id for choice in choices)
        super().__init__(
            custom_id="win5-special-scoring-target",
            placeholder=scoring_copy.SPECIAL_TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=scoring_copy.special_target_options(choices),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                scoring_copy.SPECIAL_TARGET_SELECTION_INVALID,
            )
            return
        if round_id not in self._allowed_round_ids:
            await self._adapter.send_component_error(
                interaction,
                scoring_copy.SPECIAL_TARGET_NOT_ON_PAGE,
            )
            return
        await self._adapter.preview_special_scoring(
            interaction,
            context=self._context,
            round_id=round_id,
            source_view=self.view if isinstance(self.view, discord.ui.View) else None,
        )


class Win5SpecialScoringTargetView(discord.ui.View):
    """Ephemeral complete-result target selector for Special scoring."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        choices: tuple[Win5SpecialResultTargetChoice, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.add_item(
            Win5SpecialScoringTargetSelect(
                adapter=adapter,
                context=context,
                choices=choices,
            )
        )


class Win5SpecialScoringConfirmView(discord.ui.View):
    """Final bound confirmation for one atomic Special scoring mutation."""

    def __init__(
        self,
        *,
        adapter: Win5StaffDiscordAdapter,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialScoringPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._confirmation_started = False

    def claim_confirmation(self) -> bool:
        """Allow at most one command invocation from this Special scoring preview."""

        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True

    @discord.ui.button(
        label=scoring_copy.CONFIRM_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id="win5-special-scoring-confirm",
    )
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.confirm_special_scoring(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self,
        )

    @discord.ui.button(
        label=scoring_copy.CANCEL_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-special-scoring-cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._adapter.cancel_special_scoring(
            interaction,
            context=self._context,
            source_view=self,
        )


@dataclass(frozen=True, slots=True)
class Win5StaffDiscordAdapter:
    """Orchestrate staff Round creation/deletion/lifecycle/result/scoring without persistence ownership."""

    queries: Win5StaffResultQueries
    commands: Win5NormalResultCommands
    special_commands: Win5SpecialResultCommands
    scoring_commands: Win5NormalScoringCommands
    special_scoring_commands: Win5SpecialScoringCommands
    special_void_queries: Win5StaffSpecialVoidQueries
    special_void_commands: Win5SpecialVoidCommands
    creation_queries: Win5StaffRoundCreationQueries
    creation_commands: Win5RoundCreationCommands
    lifecycle_queries: Win5StaffRoundLifecycleQueries
    lifecycle_commands: Win5RoundLifecycleCommands
    season_queries: Win5StaffSeasonQueries
    season_commands: Win5SeasonLifecycleCommands
    deletion_queries: Win5StaffRoundDeletionQueries
    deletion_commands: Win5SetupRoundDeletionCommands
    authorize_interaction: AuthorizeDiscordInteraction
    prepare_command: PrepareDiscordCommand
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        command_name: str = _COMMAND_NAME,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                common_copy.BOUND_CONTINUE,
            )
            return False
        try:
            return await self.authorize_interaction(interaction, command_name)
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                common_copy.authorization_error(correlation_id(interaction)),
            )
            return False

    async def show_round_panel(self, interaction: discord.Interaction) -> None:
        """Authorize and open the incremental `/win5 staff round` panel."""

        try:
            if not await self.authorize_interaction(interaction, _COMMAND_NAME):
                return
            context = Win5StaffInteractionContext.from_interaction(interaction)
            await interaction.response.send_message(
                common_copy.ROUND_PANEL_PROMPT,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                view=Win5StaffRoundActionView(adapter=self, context=context),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                common_copy.round_panel_error(correlation_id(interaction)),
            )

    async def show_season_panel(self, interaction: discord.Interaction) -> None:
        """Authorize and open the independent `/win5 staff season` panel."""

        try:
            if not await self.authorize_interaction(interaction, _SEASON_COMMAND_NAME):
                return
            context = Win5StaffInteractionContext.from_interaction(interaction)
            await interaction.response.send_message(
                common_copy.SEASON_PANEL_PROMPT,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                view=Win5StaffSeasonActionView(adapter=self, context=context),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                common_copy.season_panel_error(correlation_id(interaction)),
            )

    async def show_season_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        offset: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the Season panel with one fresh bounded target page."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SEASON_COMMAND_NAME,
        ):
            return
        try:
            page = await self.blocking_runner(
                lambda: self.season_queries.list_season_targets(
                    action=action,
                    offset=offset,
                    limit=_SEASON_PAGE_SIZE,
                )
            )
        except Win5StaffSeasonQueryError as error:
            await self.send_component_error(interaction, _season_query_error_message(error))
            return
        except (TypeError, ValueError):
            await self.send_component_error(
                interaction,
                season_copy.TARGET_PAGE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                season_copy.target_load_error(correlation_id(interaction)),
            )
            return
        if not page.choices:
            await self.send_component_error(
                interaction,
                season_copy.no_targets(action),
            )
            return
        replacement_view = Win5SeasonTargetView(adapter=self, context=context, page=page)
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=season_copy.target_page_prompt(
                    action=action,
                    page_number=page.offset // page.limit + 1,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "season-target-selector")
            await self._send_season_transition_error(interaction)

    async def open_season_metadata_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        target: Win5SeasonSnapshot | None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open a create/edit Modal after current bound authorization."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SEASON_COMMAND_NAME,
        ):
            return
        try:
            current_target = target
            if action == Win5SeasonAction.EDIT:
                if target is None:
                    raise ValueError(season_copy.EDIT_TARGET_REQUIRED)
                current_target = await self.blocking_runner(
                    lambda: self.season_queries.get_season_target(
                        season_id=target.id,
                        expected_action=Win5SeasonAction.EDIT,
                    )
                )
            await interaction.response.send_modal(
                Win5SeasonMetadataModal(
                    adapter=self,
                    context=context,
                    action=action,
                    target=current_target,
                    source_view=source_view,
                )
            )
        except Win5StaffSeasonQueryError as error:
            await self.send_component_error(interaction, _season_query_error_message(error))
        except (TypeError, ValueError) as error:
            await self.send_component_error(interaction, season_copy.modal_error(error))
        except Exception:
            self._log_delivery_failure(interaction, "season-metadata-modal")

    async def preview_season_metadata(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        target: Win5SeasonSnapshot | None,
        name: str,
        starts_at: str,
        ends_at: str,
        reason: str | None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Parse metadata into an adapter-local Preview without a canonical write."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                season_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _SEASON_COMMAND_NAME, ephemeral=True):
            return
        try:
            current_target = target
            if action == Win5SeasonAction.EDIT:
                if target is None:
                    raise ValueError(season_copy.EDIT_TARGET_REQUIRED)
                current_target = await self.blocking_runner(
                    lambda: self.season_queries.get_season_target(
                        season_id=target.id,
                        expected_action=Win5SeasonAction.EDIT,
                    )
                )
            preview = Win5SeasonMutationPreview(
                action=action,
                target=current_target,
                desired_name=name.strip(),
                desired_starts_at=_parse_optional_kst_datetime(starts_at, field_label=season_copy.START_FIELD_LABEL),
                desired_ends_at=_parse_optional_kst_datetime(ends_at, field_label=season_copy.END_FIELD_LABEL),
                reason=reason,
            )
            message = format_season_preview(preview)
        except Win5StaffSeasonQueryError as error:
            await self._edit_deferred_season_transition(
                interaction,
                content=_season_query_error_message(error),
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_season_transition(
                interaction,
                content=season_copy.preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_season_transition(
                interaction,
                content=season_copy.preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_season_transition(
            interaction,
            content=message,
            view=Win5SeasonConfirmView(adapter=self, context=context, preview=preview),
            source_view=source_view,
        )

    async def preview_season_transition(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5SeasonAction,
        season_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Freshly revalidate one transition target and render a zero-write Preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                season_copy.BOUND_TARGET,
            )
            return
        if not await self.prepare_command(interaction, _SEASON_COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.season_queries.get_season_target(
                    season_id=season_id,
                    expected_action=action,
                )
            )
            preview = Win5SeasonMutationPreview(action=action, target=target)
            message = format_season_preview(preview)
        except Win5StaffSeasonQueryError as error:
            await self._edit_deferred_season_transition(
                interaction,
                content=_season_query_error_message(error),
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_season_transition(
                interaction,
                content=season_copy.preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_season_transition(
                interaction,
                content=season_copy.preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_season_transition(
            interaction,
            content=message,
            view=Win5SeasonConfirmView(adapter=self, context=context, preview=preview),
            source_view=source_view,
        )

    async def confirm_season_mutation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SeasonMutationPreview,
        source_view: Win5SeasonConfirmView,
    ) -> None:
        """Execute the final Season command through one fresh command UoW."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                season_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _SEASON_COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_season_notice(
                interaction,
                season_copy.CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            command = preview.to_command(
                idempotency_key=f"win5-season-{preview.action.value}:{interaction_id}",
                actor_discord_user_id=str(actor_id),
                guild_id=str(context.guild_id),
                correlation_id=str(interaction_id),
            )
            if isinstance(command, CreateWin5Season):
                result = await self.blocking_runner(lambda: self.season_commands.create_season(command))
            elif isinstance(command, UpdateWin5SeasonMetadata):
                result = await self.blocking_runner(lambda: self.season_commands.update_metadata(command))
            else:
                result = await self.blocking_runner(lambda: self.season_commands.transition_season(command))
        except Win5SeasonLifecycleError as error:
            await self._edit_deferred(interaction, content=_season_command_error_message(error))
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=season_copy.mutation_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=season_copy.mutation_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(interaction, content=format_season_success(result))

    async def cancel_season_mutation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a Season Preview without a canonical write."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SEASON_COMMAND_NAME,
        ):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=season_copy.CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "season-preview-cancel")

    async def show_round_creation_seasons(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_type: Win5RoundType,
        offset: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with one fresh eligible-Season page."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            page = await self.blocking_runner(
                lambda: self.creation_queries.list_round_creation_seasons(
                    offset=offset,
                    limit=_CREATION_SEASON_PAGE_SIZE,
                )
            )
        except Win5StaffRoundCreationQueryError:
            await self.send_component_error(
                interaction,
                creation_copy.CREATION_SOURCE_INVALID,
            )
            return
        except (TypeError, ValueError):
            await self.send_component_error(
                interaction,
                creation_copy.SEASON_PAGE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                creation_copy.season_load_error(correlation_id(interaction)),
            )
            return
        if not page.choices:
            await self.send_component_error(
                interaction,
                creation_copy.NO_ELIGIBLE_SEASON,
            )
            return
        replacement_view = Win5RoundCreationSeasonView(
            adapter=self,
            context=context,
            round_type=round_type,
            page=page,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=_round_creation_season_selector_message(
                    round_type=round_type,
                    page=page,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "round-creation-season-selector")
            await self._send_round_creation_transition_error(interaction)

    async def open_round_creation_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        season: Win5RoundCreationSeasonChoice,
        round_type: Win5RoundType,
    ) -> None:
        """Open the selected TextInput-only creation Modal without deferring."""

        if not await self._authorize_bound(interaction, context=context):
            return
        modal: discord.ui.Modal
        if round_type == Win5RoundType.NORMAL:
            modal = Win5NormalRoundCreationModal(
                adapter=self,
                context=context,
                season=season,
            )
        else:
            modal = Win5SpecialRoundCreationModal(
                adapter=self,
                context=context,
                season=season,
            )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self._log_delivery_failure(interaction, "round-creation-modal")

    async def preview_normal_round_creation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        season: Win5RoundCreationSeasonChoice,
        round_name: str,
        race_name: str,
        race_schedule: str,
        gate_numbers: str,
        horse_names: str,
        previous_draft: Win5NormalRoundCreationDraft | None,
        source_view: Win5NormalRoundCreationPreviewView | None,
    ) -> None:
        """Validate bulk input and replace it with a zero-write LayoutView."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                common_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            draft = build_normal_round_creation_draft(
                season=season,
                round_name=round_name,
                race_name=race_name,
                race_schedule=race_schedule,
                gate_numbers=gate_numbers,
                horse_names=horse_names,
            )
            format_normal_round_creation_preview(draft)
        except (TypeError, ValueError) as error:
            if previous_draft is None:
                await self._edit_deferred(
                    interaction,
                    content=creation_copy.preview_error(error),
                )
            else:
                await self._edit_deferred_normal_creation_layout(
                    interaction,
                    context=context,
                    draft=previous_draft,
                    source_view=source_view,
                    notice=creation_copy.reentry_error(error),
                )
            return
        await self._edit_deferred_normal_creation_layout(
            interaction,
            context=context,
            draft=draft,
            source_view=source_view,
        )

    async def open_normal_round_reentry_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Open one prefilled bulk replacement Modal without retaining a UoW."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5NormalRoundCreationModal(
                    adapter=self,
                    context=context,
                    season=draft.season,
                    draft=draft,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-creation-reentry-modal")

    async def open_normal_round_entry_correction_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        entry: Win5NormalRoundCreationDraftEntry,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Open a prefilled row correction Modal after current authorization."""

        if not await self._authorize_bound(interaction, context=context):
            return
        if not any(candidate.row_id == entry.row_id for candidate in draft.entries):
            await self.send_component_error(
                interaction,
                creation_copy.ENTRY_NOT_IN_DRAFT,
            )
            return
        try:
            await interaction.response.send_modal(
                Win5NormalRoundEntryCorrectionModal(
                    adapter=self,
                    context=context,
                    draft=draft,
                    entry=entry,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-entry-correction-modal")

    async def apply_normal_round_entry_correction(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        row_id: int,
        gate_number: str,
        horse_name: str,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Replace one draft row and fresh-render without a command or query UoW."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                creation_copy.BOUND_CORRECTION_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            gate_text = gate_number.strip()
            if re.fullmatch(r"\d+", gate_text) is None or int(gate_text) <= 0:
                raise ValueError(creation_copy.GATE_POSITIVE)
            corrected = draft.corrected(
                row_id=row_id,
                gate_number=int(gate_text),
                horse_name=horse_name,
            )
            format_normal_round_creation_preview(corrected)
        except (TypeError, ValueError) as error:
            detail = str(error)
            if "gate numbers must be unique" in detail:
                detail = creation_copy.DUPLICATE_GATE
            elif "entry name" in detail:
                detail = creation_copy.HORSE_NAME_INVALID
            await self._edit_deferred_normal_creation_layout(
                interaction,
                context=context,
                draft=draft,
                source_view=source_view,
                notice=creation_copy.correction_error(detail),
            )
            return
        await self._edit_deferred_normal_creation_layout(
            interaction,
            context=context,
            draft=corrected,
            source_view=source_view,
            notice=creation_copy.ENTRY_CORRECTION_APPLIED,
        )

    async def show_normal_round_creation_page(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Render another bounded page while preserving the full immutable draft."""

        if not await self._authorize_bound(interaction, context=context):
            return
        await self._replace_normal_creation_layout(
            interaction,
            context=context,
            draft=draft,
            source_view=source_view,
        )

    async def confirm_normal_round_creation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Invoke the existing atomic command only from Final Confirm."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                creation_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not draft.all_pages_reviewed:
            await self._edit_deferred_normal_creation_layout(
                interaction,
                context=context,
                draft=draft,
                source_view=source_view,
                notice=creation_copy.REVIEW_ALL_PAGES,
            )
            return
        if not source_view.claim_confirmation():
            await self._edit_deferred(
                interaction,
                content=creation_copy.CONFIRM_ALREADY_STARTED,
            )
            return
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            command = draft.to_command(
                idempotency_key=f"win5-normal-round-create:{interaction_id}",
                actor_discord_user_id=str(actor_id),
                guild_id=str(context.guild_id),
                correlation_id=str(interaction_id),
            )
            result = await self.blocking_runner(lambda: self.creation_commands.create_round(command))
        except Win5RoundCreationError as error:
            message = _creation_command_error_message(error)
        except (TypeError, ValueError) as error:
            message = creation_copy.creation_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = creation_copy.creation_internal_error(correlation_id(interaction))
        else:
            message = format_round_creation_success(result)
        await self._edit_deferred_normal_creation_terminal(
            interaction,
            message=message,
            source_view=source_view,
        )

    async def cancel_normal_round_creation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        """Discard the local draft with no command, query, audit, or write."""

        if not await self._authorize_bound(interaction, context=context):
            return
        terminal_view = _normal_creation_terminal_layout(creation_copy.NORMAL_CREATION_CANCELLED)
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=terminal_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-creation-cancel")

    async def create_special_round(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        season_id: int,
        round_name: str,
        race_names: str,
        reason: str | None,
    ) -> None:
        """Create a Special Round from one bound Modal submission."""

        await self._create_special_round(
            interaction,
            context=context,
            season_id=season_id,
            round_name=round_name,
            race_names=race_names,
            reason=reason,
        )

    async def _create_special_round(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        season_id: int,
        round_name: str,
        race_names: str,
        reason: str | None,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                common_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            parsed_races = tuple(
                Win5RoundCreationRaceInput(name=name) for name in parse_special_round_race_names(race_names)
            )
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.creation_commands.create_round(
                    CreateWin5Round(
                        season_id=season_id,
                        round_type=Win5RoundType.SPECIAL,
                        round_name=round_name,
                        races=parsed_races,
                        idempotency_key=f"win5-special-round-create:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                        reason=reason,
                    )
                )
            )
        except Win5RoundCreationError as error:
            await self._edit_deferred(
                interaction,
                content=_creation_command_error_message(error),
            )
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=creation_copy.creation_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=creation_copy.creation_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_round_creation_success(result),
            )

    async def show_setup_round_deletion_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        offset: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with one fresh paged deletion selector."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            page = await self.blocking_runner(
                lambda: self.deletion_queries.list_setup_round_deletion_targets(
                    offset=offset,
                    limit=_DELETION_PAGE_SIZE,
                )
            )
        except Win5StaffRoundDeletionQueryError as error:
            await self.send_component_error(interaction, _deletion_query_error_message(error))
            return
        except (TypeError, ValueError):
            await self.send_component_error(
                interaction,
                deletion_copy.TARGET_PAGE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                deletion_copy.targets_load_error(correlation_id(interaction)),
            )
            return
        if not page.choices:
            await self.send_component_error(
                interaction,
                deletion_copy.NO_TARGETS,
            )
            return
        page_number = page.offset // page.limit + 1
        replacement_view = Win5SetupRoundDeletionTargetView(
            adapter=self,
            context=context,
            page=page,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=deletion_copy.target_page_prompt(page_number),
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "setup-round-deletion-selector")
            await self._send_setup_deletion_transition_error(interaction)

    async def open_setup_round_deletion_reason_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open the required-reason Modal without retaining query state."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5SetupRoundDeletionReasonModal(
                    adapter=self,
                    context=context,
                    round_id=round_id,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(interaction, "setup-round-deletion-reason-modal")

    async def preview_setup_round_deletion(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        reason: str,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Reauthorize and read one exact graph into a destructive preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                deletion_copy.BOUND_INPUT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        normalized_reason = reason.strip()
        if not normalized_reason:
            await self._edit_deferred_setup_deletion_transition(
                interaction,
                content=deletion_copy.REASON_REQUIRED,
                source_view=source_view,
            )
            return
        try:
            snapshot = await self.blocking_runner(
                lambda: self.deletion_queries.get_setup_round_deletion_target(round_id=round_id)
            )
            preview = Win5SetupRoundDeletionPreview(snapshot=snapshot, reason=normalized_reason)
        except Win5StaffRoundDeletionQueryError as error:
            await self._edit_deferred_setup_deletion_transition(
                interaction,
                content=_deletion_query_error_message(error),
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_setup_deletion_transition(
                interaction,
                content=deletion_copy.preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_setup_deletion_transition(
                interaction,
                content=deletion_copy.preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_setup_deletion_transition(
            interaction,
            content=format_setup_round_deletion_preview(preview),
            view=Win5SetupRoundDeletionConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def confirm_setup_round_deletion(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SetupRoundDeletionPreview,
        source_view: Win5SetupRoundDeletionConfirmView,
    ) -> None:
        """Execute the guarded graph deletion in one final command UoW."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                deletion_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_setup_deletion_notice(
                interaction,
                deletion_copy.CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            snapshot = preview.snapshot
            result = await self.blocking_runner(
                lambda: self.deletion_commands.delete_round(
                    DeleteWin5SetupRound(
                        season_id=snapshot.season_id,
                        round_id=snapshot.round_id,
                        expected_graph_fingerprint=snapshot.graph_fingerprint,
                        idempotency_key=f"win5-setup-round-delete:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        reason=preview.reason,
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5SetupRoundDeletionError as error:
            await self._edit_deferred(
                interaction,
                content=_deletion_command_error_message(error),
            )
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=deletion_copy.deletion_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=deletion_copy.deletion_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_setup_round_deletion_success(result),
            )

    async def cancel_setup_round_deletion(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Close a deletion preview without calling Application."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=deletion_copy.CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "setup-round-deletion-cancel")

    async def show_round_lifecycle_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5RoundLifecycleAction,
        offset: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with one fresh paged lifecycle selector."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            page = await self.blocking_runner(
                lambda: self.lifecycle_queries.list_round_lifecycle_targets(
                    action=action,
                    offset=offset,
                    limit=_LIFECYCLE_PAGE_SIZE,
                )
            )
        except Win5StaffRoundLifecycleQueryError as error:
            await self.send_component_error(
                interaction,
                _lifecycle_query_error_message(error),
            )
            return
        except (TypeError, ValueError):
            await self.send_component_error(
                interaction,
                lifecycle_copy.TARGET_PAGE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                lifecycle_copy.targets_load_error(correlation_id(interaction)),
            )
            return
        if not page.choices:
            await self.send_component_error(
                interaction,
                lifecycle_copy.no_targets(action),
            )
            return
        page_number = page.offset // page.limit + 1
        replacement_view = Win5RoundLifecycleTargetView(
            adapter=self,
            context=context,
            page=page,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=lifecycle_copy.target_page_prompt(action=action, page_number=page_number),
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "lifecycle-target-selector")
            await self._send_round_lifecycle_transition_error(interaction)

    async def preview_round_lifecycle(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        action: Win5RoundLifecycleAction,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Reauthorize a selected Round and render a zero-write confirmation."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                lifecycle_copy.BOUND_TARGET,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.lifecycle_queries.get_round_lifecycle_target(
                    round_id=round_id,
                    expected_action=action,
                )
            )
            preview = Win5RoundLifecyclePreview(target=target)
        except Win5StaffRoundLifecycleQueryError as error:
            await self._edit_deferred_round_lifecycle_transition(
                interaction,
                content=_lifecycle_query_error_message(error),
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_round_lifecycle_transition(
                interaction,
                content=lifecycle_copy.preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_round_lifecycle_transition(
                interaction,
                content=lifecycle_copy.preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_round_lifecycle_transition(
            interaction,
            content=format_round_lifecycle_preview(preview),
            view=Win5RoundLifecycleConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def confirm_round_lifecycle(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5RoundLifecyclePreview,
        source_view: Win5RoundLifecycleConfirmView,
    ) -> None:
        """Execute one final Round transition in one command UoW."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                lifecycle_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_round_lifecycle_notice(
                interaction,
                lifecycle_copy.CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            action = preview.target.action
            result = await self.blocking_runner(
                lambda: self.lifecycle_commands.transition_round(
                    TransitionWin5Round(
                        round_id=preview.target.round_id,
                        action=action,
                        idempotency_key=f"win5-round-{action.value}:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5RoundLifecycleError as error:
            await self._edit_deferred(
                interaction,
                content=_lifecycle_command_error_message(error),
            )
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=lifecycle_copy.mutation_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=lifecycle_copy.mutation_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_round_lifecycle_success(preview, result),
            )

    async def cancel_round_lifecycle(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a lifecycle preview without calling Application."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=lifecycle_copy.CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "lifecycle-preview-cancel")

    async def show_normal_result_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with a bounded current target selector."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(lambda: self.queries.list_normal_result_targets(mode=mode, limit=25))
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                results_copy.normal_targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                results_copy.no_normal_targets(mode),
            )
            return
        replacement_view = Win5NormalResultTargetView(
            adapter=self,
            context=context,
            mode=mode,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=results_copy.NORMAL_TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "target-selector")
            await self._send_normal_result_transition_error(interaction)

    async def show_special_result_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5SpecialResultTargetMode,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with a bounded Special Round selector."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(lambda: self.queries.list_special_result_targets(mode=mode, limit=25))
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                results_copy.special_targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                results_copy.no_special_targets(mode),
            )
            return
        replacement_view = Win5SpecialResultTargetView(
            adapter=self,
            context=context,
            mode=mode,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=results_copy.SPECIAL_TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-target-selector")
            await self._send_special_result_transition_error(interaction)

    async def show_special_void_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with mutable closed Special Rounds."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(lambda: self.special_void_queries.list_targets(limit=25))
        except Win5StaffSpecialVoidQueryError:
            await self.send_component_error(
                interaction,
                void_copy.TARGET_SOURCE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                void_copy.targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                void_copy.NO_TARGETS,
            )
            return
        replacement_view = Win5SpecialVoidTargetView(
            adapter=self,
            context=context,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=void_copy.TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-void-target-selector")
            await self._send_special_void_transition_error(interaction)

    async def show_special_round_cancellation_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with whole-Round cancellation targets."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(lambda: self.special_void_queries.list_targets(limit=25))
        except Win5StaffSpecialVoidQueryError:
            await self.send_component_error(
                interaction,
                void_copy.WHOLE_TARGET_SOURCE_INVALID,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                void_copy.whole_targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                void_copy.NO_WHOLE_TARGETS,
            )
            return
        replacement_view = Win5SpecialRoundCancellationTargetView(
            adapter=self,
            context=context,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=void_copy.WHOLE_TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-round-cancellation-target-selector")
            await self._send_special_round_cancellation_transition_error(interaction)

    async def show_special_void_races(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        page: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Fresh-query and render one bounded Race page."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            target = await self.blocking_runner(lambda: self.special_void_queries.get_target(round_id=round_id))
            visible = _special_void_race_page(target, page=page)
            page_count = _special_void_race_page_count(target)
        except Win5StaffSpecialVoidQueryError:
            await self.send_component_error(
                interaction,
                void_copy.ROUND_STALE,
            )
            return
        except (TypeError, ValueError) as error:
            await self.send_component_error(
                interaction,
                void_copy.race_page_error(error),
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                void_copy.races_load_error(correlation_id(interaction)),
            )
            return
        start = page * _SPECIAL_VOID_RACE_PAGE_SIZE
        content = void_copy.race_page_content(
            target=target,
            page=page,
            page_count=page_count,
            start=start,
            visible_count=len(visible),
        )
        replacement_view = Win5SpecialVoidRaceView(
            adapter=self,
            context=context,
            target=target,
            page=page,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-void-race-selector")
            await self._send_special_void_transition_error(interaction)

    async def open_special_void_reason_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        target: Win5StaffSpecialVoidTarget,
        race_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open a required-reason Modal without retaining a Session or lock."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5SpecialVoidReasonModal(
                    adapter=self,
                    context=context,
                    target=target,
                    race_id=race_id,
                    source_view=source_view,
                )
            )
        except (TypeError, ValueError) as error:
            await self.send_component_error(interaction, str(error))
        except Exception:
            self._log_delivery_failure(interaction, "special-void-reason-modal")

    async def preview_special_void_change(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        selected_target: Win5StaffSpecialVoidTarget,
        race_id: int,
        reason: str,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Revalidate a Modal selection and render a zero-write impact preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                common_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.special_void_queries.get_target(round_id=selected_target.round_id)
            )
            if target.void_fingerprint != selected_target.void_fingerprint:
                raise ValueError(void_copy.VOID_FINGERPRINT_STALE)
            selected_race = next((race for race in selected_target.races if race.id == race_id), None)
            current_race = next((race for race in target.races if race.id == race_id), None)
            if selected_race is None or current_race is None or selected_race.is_void != current_race.is_void:
                raise ValueError(void_copy.SELECTED_RACE_STALE)
            preview = Win5SpecialVoidPreview(
                target=target,
                race_id=race_id,
                target_voided=not current_race.is_void,
                reason=reason,
            )
            content = format_special_void_preview(preview)
        except Win5StaffSpecialVoidQueryError:
            await self._edit_deferred_special_void_transition(
                interaction,
                content=void_copy.RACE_STATE_STALE,
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_special_void_transition(
                interaction,
                content=void_copy.preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_special_void_transition(
                interaction,
                content=void_copy.preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_special_void_transition(
            interaction,
            content=content,
            view=Win5SpecialVoidConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def open_special_round_cancellation_reason_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open the required shared-reason Modal without retaining persistence state."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5SpecialRoundCancellationReasonModal(
                    adapter=self,
                    context=context,
                    round_id=round_id,
                    source_view=source_view,
                )
            )
        except (TypeError, ValueError) as error:
            await self.send_component_error(interaction, str(error))
        except Exception:
            self._log_delivery_failure(interaction, "special-round-cancellation-reason-modal")

    async def preview_special_round_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        reason: str,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Fresh-query and render an atomic all-void consequence preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                common_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(lambda: self.special_void_queries.get_target(round_id=round_id))
            preview = Win5SpecialRoundCancellationPreview(target=target, reason=reason)
            content = format_special_round_cancellation_preview(preview)
        except Win5StaffSpecialVoidQueryError:
            await self._edit_deferred_special_round_cancellation_transition(
                interaction,
                content=void_copy.WHOLE_STATE_STALE,
                source_view=source_view,
            )
            return
        except (TypeError, ValueError) as error:
            await self._edit_deferred_special_round_cancellation_transition(
                interaction,
                content=void_copy.whole_preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_special_round_cancellation_transition(
                interaction,
                content=void_copy.whole_preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_special_round_cancellation_transition(
            interaction,
            content=content,
            view=Win5SpecialRoundCancellationConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def show_normal_scoring_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with complete-result Normal scoring targets."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(
                lambda: self.queries.list_normal_result_targets(
                    mode=Win5NormalResultTargetMode.CORRECTION,
                    limit=25,
                )
            )
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                scoring_copy.normal_targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                scoring_copy.NO_NORMAL_TARGETS,
            )
            return
        replacement_view = Win5NormalScoringTargetView(
            adapter=self,
            context=context,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=scoring_copy.NORMAL_TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "scoring-target-selector")
            await self._send_normal_scoring_transition_error(interaction)

    async def show_special_scoring_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Replace the action panel with complete-result Special scoring targets."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            choices = await self.blocking_runner(
                lambda: self.queries.list_special_result_targets(
                    mode=Win5SpecialResultTargetMode.CORRECTION,
                    limit=25,
                )
            )
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                scoring_copy.special_targets_load_error(correlation_id(interaction)),
            )
            return
        if not choices:
            await self.send_component_error(
                interaction,
                scoring_copy.NO_SPECIAL_TARGETS,
            )
            return
        replacement_view = Win5SpecialScoringTargetView(
            adapter=self,
            context=context,
            choices=choices,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=scoring_copy.SPECIAL_TARGET_PROMPT,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-scoring-target-selector")
            await self._send_special_scoring_transition_error(interaction)

    async def open_normal_result_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open a TextInput-only result Modal without deferring first."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5NormalResultModal(
                    adapter=self,
                    context=context,
                    mode=mode,
                    round_id=round_id,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(interaction, "result-modal")

    async def preview_normal_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5NormalResultTargetMode,
        round_id: int,
        result_order: str,
        reason: str | None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Reauthorize Modal submit and render a zero-write confirmation preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                results_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            gate_numbers = parse_normal_result_gate_numbers(result_order)
            target = await self.blocking_runner(
                lambda: self.queries.get_normal_result_target(
                    round_id=round_id,
                    expected_mode=mode,
                )
            )
            entries_by_gate = {entry.gate_number: entry for entry in target.entries}
            if any(gate_number not in entries_by_gate for gate_number in gate_numbers):
                raise ValueError(results_copy.GATE_NOT_IN_ENTRIES)
            preview = Win5NormalResultPreview(
                target=target,
                placements=tuple(
                    Win5NormalResultPlacementInput(
                        position=position,
                        race_entry_id=entries_by_gate[gate_number].id,
                    )
                    for position, gate_number in enumerate(gate_numbers, start=1)
                ),
                reason=reason,
            )
        except Win5StaffResultQueryError as error:
            await self._edit_deferred_normal_result_transition(
                interaction,
                content=self._target_query_error_message(error),
                source_view=source_view,
            )
            return
        except ValueError as error:
            await self._edit_deferred_normal_result_transition(
                interaction,
                content=results_copy.normal_preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_normal_result_transition(
                interaction,
                content=results_copy.normal_preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_normal_result_transition(
            interaction,
            content=format_normal_result_preview(preview),
            view=Win5NormalResultConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def show_special_result_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5SpecialResultTargetMode,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Load and show the complete current Race/void mapping before input."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.queries.get_special_result_target(
                    round_id=round_id,
                    expected_mode=mode,
                )
            )
            content = format_special_result_editor(target)
        except Win5StaffResultQueryError as error:
            await self.send_component_error(
                interaction,
                self._special_target_query_error_message(error),
            )
            return
        except ValueError as error:
            await self.send_component_error(
                interaction,
                results_copy.special_editor_error(error),
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(
                interaction,
                results_copy.special_editor_internal_error(correlation_id(interaction)),
            )
            return
        replacement_view = Win5SpecialResultEditorView(
            adapter=self,
            context=context,
            target=target,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.response.edit_message(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-result-editor")
            await self._send_special_result_transition_error(interaction)

    async def open_special_result_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        target: Win5SpecialResultTarget,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Open a TextInput-only Special result Modal without deferring first."""

        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                Win5SpecialResultModal(
                    adapter=self,
                    context=context,
                    target=target,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-result-modal")

    async def preview_special_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        mode: Win5SpecialResultTargetMode,
        round_id: int,
        winner_gates: str,
        reason: str | None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Requery the Race order and render one complete zero-write preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                results_copy.BOUND_MODAL_SUBMIT,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.queries.get_special_result_target(
                    round_id=round_id,
                    expected_mode=mode,
                )
            )
            gate_numbers = parse_special_result_gate_numbers(
                winner_gates,
                expected_count=len(target.non_void_races),
            )
            preview = Win5SpecialResultPreview(
                target=target,
                winners=tuple(
                    Win5SpecialResultWinnerInput(
                        race_id=race.id,
                        gate_number=gate_number,
                    )
                    for race, gate_number in zip(target.non_void_races, gate_numbers, strict=True)
                ),
                reason=reason,
            )
            content = format_special_result_preview(preview)
        except Win5StaffResultQueryError as error:
            await self._edit_deferred_special_result_transition(
                interaction,
                content=self._special_target_query_error_message(error),
                source_view=source_view,
            )
            return
        except ValueError as error:
            await self._edit_deferred_special_result_transition(
                interaction,
                content=results_copy.special_preview_error(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_special_result_transition(
                interaction,
                content=results_copy.special_preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_special_result_transition(
            interaction,
            content=content,
            view=Win5SpecialResultConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def confirm_special_void_change(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialVoidPreview,
        source_view: Win5SpecialVoidConfirmView,
    ) -> None:
        """Execute one final Race void/restore command with interaction idempotency."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                void_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_special_void_notice(
                interaction,
                void_copy.CHANGE_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.special_void_commands.set_race_void(
                    SetSpecialWin5RaceVoid(
                        round_id=preview.target.round_id,
                        race_id=preview.race_id,
                        voided=preview.target_voided,
                        expected_void_fingerprint=preview.target.void_fingerprint,
                        idempotency_key=f"win5-special-void:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        reason=preview.reason,
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5SpecialVoidError as error:
            await self._edit_deferred(
                interaction,
                content=_special_void_command_error_message(error),
            )
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=void_copy.mutation_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=void_copy.mutation_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_special_void_success(preview, result),
            )

    async def confirm_special_round_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialRoundCancellationPreview,
        source_view: Win5SpecialRoundCancellationConfirmView,
    ) -> None:
        """Execute one atomic all-void cancellation with interaction idempotency."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                void_copy.BOUND_WHOLE_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_special_void_notice(
                interaction,
                void_copy.WHOLE_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.special_void_commands.cancel_round(
                    CancelSpecialWin5Round(
                        round_id=preview.target.round_id,
                        expected_void_fingerprint=preview.target.void_fingerprint,
                        idempotency_key=f"win5-special-round-cancel:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        reason=preview.reason,
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5SpecialVoidError as error:
            await self._edit_deferred(
                interaction,
                content=_special_round_cancellation_error_message(error),
            )
        except (TypeError, ValueError) as error:
            await self._edit_deferred(
                interaction,
                content=void_copy.whole_mutation_error(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=void_copy.whole_mutation_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_special_round_cancellation_success(preview, result),
            )

    async def confirm_normal_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5NormalResultPreview,
        source_view: Win5NormalResultConfirmView,
    ) -> None:
        """Execute the final mutation using the confirmation interaction ID."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                results_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_normal_result_notice(
                interaction,
                results_copy.NORMAL_CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.save_result(
                    SaveNormalWin5Result(
                        round_id=preview.target.round_id,
                        placements=preview.placements,
                        expected_result_fingerprint=preview.target.result_fingerprint,
                        idempotency_key=f"win5-normal-result:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                        reason=preview.reason,
                    )
                )
            )
        except Win5NormalResultCommandError as error:
            await self._edit_deferred(
                interaction,
                content=_result_command_error_message(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=results_copy.normal_save_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_normal_result_success(preview, result),
            )

    async def confirm_special_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialResultPreview,
        source_view: Win5SpecialResultConfirmView,
    ) -> None:
        """Execute the complete Special result mutation with the final interaction ID."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                results_copy.BOUND_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_special_result_notice(
                interaction,
                results_copy.SPECIAL_CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.special_commands.save_result(
                    SaveSpecialWin5Result(
                        round_id=preview.target.round_id,
                        winners=preview.winners,
                        expected_result_fingerprint=preview.target.result_fingerprint,
                        idempotency_key=f"win5-special-result:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                        reason=preview.reason,
                    )
                )
            )
        except Win5SpecialResultCommandError as error:
            await self._edit_deferred(
                interaction,
                content=_special_result_command_error_message(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=results_copy.special_save_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_special_result_success(preview, result),
            )

    async def preview_normal_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Reauthorize target selection and render a zero-write scoring preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                scoring_copy.BOUND_NORMAL_TARGET,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.queries.get_normal_result_target(
                    round_id=round_id,
                    expected_mode=Win5NormalResultTargetMode.CORRECTION,
                )
            )
            preview = Win5NormalScoringPreview(target=target)
        except Win5StaffResultQueryError as error:
            await self._edit_deferred_normal_scoring_transition(
                interaction,
                content=self._scoring_target_query_error_message(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_normal_scoring_transition(
                interaction,
                content=scoring_copy.normal_preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_normal_scoring_transition(
            interaction,
            content=format_normal_scoring_preview(preview),
            view=Win5NormalScoringConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def confirm_normal_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5NormalScoringPreview,
        source_view: Win5NormalScoringConfirmView,
    ) -> None:
        """Execute final atomic scoring using the confirmation interaction ID."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                scoring_copy.BOUND_NORMAL_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_normal_scoring_notice(
                interaction,
                scoring_copy.NORMAL_CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.scoring_commands.score_round(
                    ScoreNormalWin5Round(
                        round_id=preview.target.round_id,
                        idempotency_key=f"win5-normal-scoring:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5NormalScoringError as error:
            await self._edit_deferred(
                interaction,
                content=_scoring_command_error_message(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=scoring_copy.normal_scoring_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_normal_scoring_success(preview, result),
            )

    async def preview_special_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        round_id: int,
        source_view: discord.ui.View | None = None,
    ) -> None:
        """Reauthorize and render a complete void-aware Special scoring preview."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                scoring_copy.BOUND_SPECIAL_TARGET,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        try:
            target = await self.blocking_runner(
                lambda: self.queries.get_special_result_target(
                    round_id=round_id,
                    expected_mode=Win5SpecialResultTargetMode.CORRECTION,
                )
            )
            preview = Win5SpecialScoringPreview(target=target)
            content = format_special_scoring_preview(preview)
        except Win5StaffResultQueryError as error:
            await self._edit_deferred_special_scoring_transition(
                interaction,
                content=self._special_scoring_target_query_error_message(error),
                source_view=source_view,
            )
            return
        except ValueError as error:
            await self._edit_deferred_special_scoring_transition(
                interaction,
                content=str(error),
                source_view=source_view,
            )
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_special_scoring_transition(
                interaction,
                content=scoring_copy.special_preview_internal_error(correlation_id(interaction)),
                source_view=source_view,
            )
            return
        await self._edit_deferred_special_scoring_transition(
            interaction,
            content=content,
            view=Win5SpecialScoringConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            ),
            source_view=source_view,
        )

    async def confirm_special_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        preview: Win5SpecialScoringPreview,
        source_view: Win5SpecialScoringConfirmView,
    ) -> None:
        """Execute final atomic Special scoring using the confirmation interaction ID."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                scoring_copy.BOUND_SPECIAL_CONFIRM,
            )
            return
        if not await self.prepare_command(interaction, _COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self._send_special_scoring_notice(
                interaction,
                scoring_copy.SPECIAL_CONFIRM_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.special_scoring_commands.score_round(
                    ScoreSpecialWin5Round(
                        round_id=preview.target.round_id,
                        idempotency_key=f"win5-special-scoring:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5SpecialScoringError as error:
            await self._edit_deferred(
                interaction,
                content=_special_scoring_command_error_message(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                content=scoring_copy.special_scoring_internal_error(correlation_id(interaction)),
            )
        else:
            await self._edit_deferred(
                interaction,
                content=format_special_scoring_success(preview, result),
            )

    async def cancel_normal_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a preview without calling Application."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=results_copy.NORMAL_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "preview-cancel")

    async def cancel_normal_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a scoring preview without calling Application."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=scoring_copy.NORMAL_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "scoring-preview-cancel")

    async def cancel_special_scoring(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a Special scoring preview without mutation."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=scoring_copy.SPECIAL_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-scoring-preview-cancel")

    async def cancel_special_void_change(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a Special void preview without mutation."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=void_copy.CHANGE_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-void-preview-cancel")

    async def cancel_special_round_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close an all-void preview without mutation."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=void_copy.WHOLE_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-round-cancellation-preview-cancel")

    async def cancel_special_result(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        source_view: discord.ui.View,
    ) -> None:
        """Reauthorize and close a Special result preview without mutation."""

        if not await self._authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=results_copy.SPECIAL_CANCELLED,
                allowed_mentions=discord.AllowedMentions.none(),
                view=None,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-preview-cancel")

    async def send_component_error(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        """Send one safe private initial component/Modal error."""

        try:
            await interaction.response.send_message(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._log_delivery_failure(interaction, "component-error")

    async def _replace_normal_creation_layout(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView,
        notice: str | None = None,
    ) -> None:
        replacement_view = Win5NormalRoundCreationPreviewView(
            adapter=self,
            context=context,
            draft=draft,
            notice=notice,
        )
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-creation-preview-transition")
            await self._send_round_creation_transition_error(interaction)

    async def _edit_deferred_normal_creation_layout(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5StaffInteractionContext,
        draft: Win5NormalRoundCreationDraft,
        source_view: Win5NormalRoundCreationPreviewView | None,
        notice: str | None = None,
    ) -> None:
        replacement_view = Win5NormalRoundCreationPreviewView(
            adapter=self,
            context=context,
            draft=draft,
            notice=notice,
        )
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=replacement_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-creation-preview-response")
            await self._send_round_creation_transition_error(interaction)

    async def _edit_deferred_normal_creation_terminal(
        self,
        interaction: discord.Interaction,
        *,
        message: str,
        source_view: Win5NormalRoundCreationPreviewView,
    ) -> None:
        terminal_view = _normal_creation_terminal_layout(message)
        source_view.stop()
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=terminal_view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-round-creation-terminal-response")

    async def _edit_deferred_season_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "season-preview-transition")
            await self._send_season_transition_error(interaction)

    async def _send_season_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_season_notice(
            interaction,
            season_copy.SEASON_TRANSITION_ERROR,
        )

    async def _send_season_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "season-notice")

    async def _edit_deferred_round_lifecycle_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "round-lifecycle-preview-transition")
            await self._send_round_lifecycle_transition_error(interaction)

    async def _send_round_lifecycle_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_round_lifecycle_notice(
            interaction,
            lifecycle_copy.ROUND_LIFECYCLE_TRANSITION_ERROR,
        )

    async def _send_round_lifecycle_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "round-lifecycle-notice")

    async def _edit_deferred_setup_deletion_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "setup-round-deletion-preview-transition")
            await self._send_setup_deletion_transition_error(interaction)

    async def _send_setup_deletion_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_setup_deletion_notice(
            interaction,
            deletion_copy.SETUP_DELETION_TRANSITION_ERROR,
        )

    async def _send_setup_deletion_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "setup-round-deletion-notice")

    async def _edit_deferred_normal_result_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-result-preview-transition")
            await self._send_normal_result_transition_error(interaction)

    async def _send_normal_result_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_normal_result_notice(
            interaction,
            results_copy.NORMAL_RESULT_TRANSITION_ERROR,
        )

    async def _send_normal_result_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "normal-result-notice")

    async def _edit_deferred_special_result_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-result-preview-transition")
            await self._send_special_result_transition_error(interaction)

    async def _send_special_result_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_special_result_notice(
            interaction,
            results_copy.SPECIAL_RESULT_TRANSITION_ERROR,
        )

    async def _send_special_result_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "special-result-notice")

    async def _edit_deferred_special_void_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-void-preview-transition")
            await self._send_special_void_transition_error(interaction)

    async def _send_special_void_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_special_void_notice(
            interaction,
            void_copy.RACE_TRANSITION_ERROR,
        )

    async def _edit_deferred_special_round_cancellation_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-round-cancellation-preview-transition")
            await self._send_special_round_cancellation_transition_error(interaction)

    async def _send_special_round_cancellation_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_special_void_notice(
            interaction,
            void_copy.WHOLE_TRANSITION_ERROR,
        )

    async def _send_special_void_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "special-void-notice")

    async def _edit_deferred_normal_scoring_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "normal-scoring-preview-transition")
            await self._send_normal_scoring_transition_error(interaction)

    async def _send_normal_scoring_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_normal_scoring_notice(
            interaction,
            scoring_copy.NORMAL_SCORING_TRANSITION_ERROR,
        )

    async def _send_normal_scoring_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "normal-scoring-notice")

    async def _edit_deferred_special_scoring_transition(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
        source_view: discord.ui.View | None = None,
    ) -> None:
        if source_view is not None:
            source_view.stop()
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "special-scoring-preview-transition")
            await self._send_special_scoring_transition_error(interaction)

    async def _send_special_scoring_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._send_special_scoring_notice(
            interaction,
            scoring_copy.SPECIAL_SCORING_TRANSITION_ERROR,
        )

    async def _send_special_scoring_notice(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            self._log_delivery_failure(interaction, "special-scoring-notice")

    async def _send_round_creation_transition_error(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(
                    creation_copy.ROUND_CREATION_TRANSITION_ERROR,
                    **payload,
                )
            else:
                await interaction.response.send_message(
                    creation_copy.ROUND_CREATION_TRANSITION_ERROR,
                    **payload,
                )
        except Exception:
            self._log_delivery_failure(interaction, "round-creation-transition-error")

    async def _edit_deferred(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(interaction, "deferred-response")

    @staticmethod
    def _target_query_error_message(error: Win5StaffResultQueryError) -> str:
        return results_copy.normal_target_query_error(error)

    @staticmethod
    def _scoring_target_query_error_message(error: Win5StaffResultQueryError) -> str:
        return scoring_copy.normal_target_query_error(error)

    @staticmethod
    def _special_target_query_error_message(error: Win5StaffResultQueryError) -> str:
        return results_copy.special_target_query_error(error)

    @staticmethod
    def _special_scoring_target_query_error_message(error: Win5StaffResultQueryError) -> str:
        return scoring_copy.special_target_query_error(error)

    @staticmethod
    def _log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def _log_delivery_failure(interaction: object, response_kind: str) -> None:
        logger.error(
            "Discord response failed correlation_id=%s command=%s response_kind=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            response_kind,
        )


class Win5StaffCommandGroup(app_commands.Group):
    """Incremental `/win5 staff` group for implemented V2 staff actions."""

    def __init__(self, *, adapter: Win5StaffDiscordAdapter) -> None:
        super().__init__(name="staff", description=registration_copy.STAFF_DESCRIPTION)
        self._adapter = adapter

    @app_commands.command(name="round", description=registration_copy.STAFF_ROUND_DESCRIPTION)
    async def round(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_round_panel(interaction)

    @app_commands.command(name="season", description=registration_copy.STAFF_SEASON_DESCRIPTION)
    async def season(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_season_panel(interaction)
