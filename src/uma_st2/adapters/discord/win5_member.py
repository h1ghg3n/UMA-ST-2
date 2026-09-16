"""Discord adapter for member-facing WIN5 interactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Literal

import discord
from discord import app_commands

from uma_st2.application.win5 import (
    CancelWin5Submission,
    SavedWin5Submission,
    SaveWin5Submission,
    Win5AcceptedNormalSubmission,
    Win5AcceptedSpecialSubmission,
    Win5CancellableSubmission,
    Win5EmptyPickSetUnsupportedError,
    Win5MemberApprovalPendingError,
    Win5MemberCommandError,
    Win5MemberCommands,
    Win5MemberIdentityError,
    Win5MemberQueries,
    Win5MemberQueryApprovalPendingError,
    Win5MemberQueryError,
    Win5MemberQueryIdentityError,
    Win5MemberSubmissionRoundPage,
    Win5MemberSubmissionRoundSummary,
    Win5MemberSubmissionsDashboard,
    Win5NormalSubmissionEditor,
    Win5NormalSubmissionPick,
    Win5RaceCard,
    Win5RoundUnavailableError,
    Win5SpecialSubmissionEditor,
    Win5SpecialSubmissionPick,
    Win5StandingsSeasonUnavailableError,
    Win5SubmissionHistoryQueries,
    Win5SubmissionInvalidError,
    Win5SubmissionPickInput,
    Win5SubmissionUnavailableError,
    Win5SubmissionVersionConflictError,
)
from uma_st2.domain.win5 import (
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionTier,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
    send_deferred_response_safely,
    send_ephemeral_internal_error_after_defer_safely,
    send_private_error_after_defer_safely,
    send_private_response_after_public_defer_safely,
)
from .strings.account import MEMBER_APPROVAL_PENDING
from .strings.win5_member_browse import (
    STANDINGS_UNAVAILABLE,
    format_active_season_info,
    format_open_rounds,
    format_standings,
)
from .strings.win5_member_browse import member_info_error_message as _member_info_error_message
from .strings.win5_member_browse import standings_season_choices as _standings_season_choices
from .strings.win5_member_cancellation import (
    BACK_LABEL,
    BOUND_CANCEL_ERROR,
    CANCEL_ALREADY_STARTED,
    CANCEL_LABEL,
    CONFIRM_LABEL,
    DISMISSED,
    NO_ACCEPTED_SUBMISSION,
    REASON_TOO_LONG,
    cancel_internal_error,
    format_cancel_success,
    format_editor_cancel_confirmation,
    format_submission_cancel_confirmation,
    special_cancel_internal_error,
)
from .strings.win5_member_cancellation import (
    cancellable_query_error_message as _cancellable_query_error_message,
)
from .strings.win5_member_cancellation import (
    cancellable_submission_choices as _cancellable_submission_choices,
)
from .strings.win5_member_cancellation import (
    independent_cancel_error_message as _independent_cancel_error_message,
)
from .strings.win5_member_cancellation import normal_cancel_error_message as _normal_cancel_error_message
from .strings.win5_member_cancellation import special_cancel_error_message as _special_cancel_error_message
from .strings.win5_member_common import BOUND_INTERACTION_ERROR, authorization_error
from .strings.win5_member_history import (
    CANCELLED_LABEL,
    HISTORY_REFRESH_ERROR,
    HISTORY_TRANSITION_ERROR,
    HISTORY_TRANSITION_STARTED,
    INITIAL_STATUS_CHANGED,
    NO_DETAIL,
    OPEN_LABEL,
    PAGE_OUT_OF_RANGE,
    PAGE_UNAVAILABLE,
    REFRESH_LABEL,
    ROUND_SELECT_ERROR,
    ROUND_SELECT_PLACEHOLDER,
    ROUND_STATUS_CHANGED,
    SCORED_LABEL,
    TARGET_MOVED_NOTICE,
    Win5SubmissionsMode,
    empty_history_message,
    history_page_button_label,
    history_refresh_internal_error,
)
from .strings.win5_member_history import (
    format_submission_history_round_copy as _format_submission_history_round,
)
from .strings.win5_member_history import (
    submission_history_round_options as _submission_history_round_options,
)
from .strings.win5_member_history import submissions_query_error_message as _submissions_query_error_message
from .strings.win5_member_submission import (
    BOUND_SAVE_ERROR,
    CLEAR_PICK_LABEL,
    CURRENT_DRAFT_UNAVAILABLE_DETAIL,
    CURRENT_SELECTION_DESCRIPTION,
    EMPTY_SAVE_ERROR,
    ENTRY_INVALID,
    ENTRY_PAGE_INVALID,
    NEXT_LABEL,
    NORMAL_ACTION_ALREADY_STARTED,
    NORMAL_EMPTY_DETAIL,
    NORMAL_INVALID_DETAIL,
    NORMAL_SAVE_STALE,
    PICK_UNAVAILABLE,
    PREVIOUS_LABEL,
    SAVE_LABEL,
    SAVE_UNAVAILABLE,
    SPECIAL_ACTION_ALREADY_STARTED,
    SPECIAL_EDIT_LABEL,
    SPECIAL_EMPTY_DETAIL,
    SPECIAL_GATE_INTEGER_ERROR,
    SPECIAL_GATE_PLACEHOLDER,
    SPECIAL_GATE_RANGE_ERROR,
    SPECIAL_INVALID_DETAIL,
    SPECIAL_MODAL_STALE,
    SPECIAL_MODAL_TARGET_MISMATCH,
    SPECIAL_PAGE_APPLIED_NOTICE,
    SPECIAL_PAGE_INVALID,
    SPECIAL_SAVE_STALE,
    SUBMISSION_UNAVAILABLE_DETAIL,
    TIER_CHANGE_NOTICE,
    TIER_INVALID,
    TIER_PLACEHOLDER,
    editor_transition_error,
    entry_placeholder,
    format_normal_submission_editor_copy,
    format_special_submission_editor_copy,
    normal_save_internal_error,
    page_label,
    save_notice,
    special_modal_title,
    special_race_input_label,
    special_save_internal_error,
)
from .strings.win5_member_submission import committed_submission_receipt_notice as _committed_submission_receipt_notice
from .strings.win5_member_submission import entry_label as _entry_label
from .strings.win5_member_submission import (
    normal_editor_query_error_message as _normal_editor_query_error_message,
)
from .strings.win5_member_submission import (
    normal_submission_round_choices as _normal_submission_round_choices,
)
from .strings.win5_member_submission import special_editor_query_error_message as _special_editor_query_error_message
from .strings.win5_member_submission import (
    special_submission_round_choices as _special_submission_round_choices,
)
from .strings.win5_registration import (
    CANCEL_DESCRIPTION,
    CANCEL_REASON_DESCRIPTION,
    CANCEL_SUBMISSION_DESCRIPTION,
    INFO_DESCRIPTION,
    RANKING_OPTION_NAME,
    REASON_OPTION_NAME,
    ROOT_DESCRIPTION,
    ROUND_ID_OPTION_NAME,
    ROUNDS_DESCRIPTION,
    SEASON_ID_OPTION_NAME,
    SPECIAL_SUBMIT_DESCRIPTION,
    SPECIAL_SUBMIT_ROUND_DESCRIPTION,
    STANDINGS_DESCRIPTION,
    STANDINGS_RANKING_DESCRIPTION,
    STANDINGS_SEASON_DESCRIPTION,
    SUBMISSION_ID_OPTION_NAME,
    SUBMISSIONS_DESCRIPTION,
    SUBMIT_DESCRIPTION,
    SUBMIT_ROUND_DESCRIPTION,
)

_INFO_COMMAND_NAME = "win5.info"
_ROUNDS_COMMAND_NAME = "win5.rounds"
_SUBMIT_COMMAND_NAME = "win5.submit"
_SPECIAL_SUBMIT_COMMAND_NAME = "win5.special-submit"
_SUBMISSIONS_COMMAND_NAME = "win5.submissions"
_CANCEL_COMMAND_NAME = "win5.cancel"
_STANDINGS_COMMAND_NAME = "win5.standings"
_COMPONENT_TIMEOUT_SECONDS = 600
_ENTRY_PAGE_SIZE = 23
_SPECIAL_RACE_PAGE_SIZE = 5
_SUBMISSION_HISTORY_PAGE_SIZE = 5
_MAX_GATE_NUMBER = 2_147_483_647
_CLEAR_PICK_VALUE = "clear"
_TIER_POSITION_COUNT = {
    Win5SubmissionTier.TOP1: 1,
    Win5SubmissionTier.TOP3: 3,
    Win5SubmissionTier.TOP5: 5,
}

logger = logging.getLogger(__name__)


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Discord interaction has no valid {field_name}.")
    return value


@dataclass(frozen=True, slots=True)
class Win5MemberInteractionContext:
    """User/guild/channel binding captured by the opening member interaction."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> Win5MemberInteractionContext:
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


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionDraft:
    """Adapter-local unsaved state for one Normal Submission editor."""

    tier: Win5SubmissionTier | None
    picks: tuple[Win5NormalSubmissionPick, ...] = field(default_factory=tuple)
    page: int = 0
    notice: str | None = None

    @classmethod
    def from_editor(cls, editor: Win5NormalSubmissionEditor) -> Win5NormalSubmissionDraft:
        if editor.submission is None:
            return cls(tier=None)
        return cls(
            tier=editor.submission.tier,
            picks=editor.submission.picks,
        )

    @property
    def picks_by_position(self) -> dict[int, int]:
        return {pick.position: pick.race_entry_id for pick in self.picks}


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionDraft:
    """Adapter-local unsaved state for one Special Submission editor."""

    picks: tuple[Win5SpecialSubmissionPick, ...] = field(default_factory=tuple)
    page: int = 0
    notice: str | None = None

    @classmethod
    def from_editor(cls, editor: Win5SpecialSubmissionEditor) -> Win5SpecialSubmissionDraft:
        if editor.submission is None:
            return cls()
        return cls(picks=editor.submission.picks)

    @property
    def picks_by_race_id(self) -> dict[int, int]:
        return {pick.race_id: pick.gate_number for pick in self.picks}


def _page_count(editor: Win5NormalSubmissionEditor) -> int:
    return max(1, (len(editor.entries) + _ENTRY_PAGE_SIZE - 1) // _ENTRY_PAGE_SIZE)


def _normal_submission_entry_options(
    *,
    editor: Win5NormalSubmissionEditor,
    draft: Win5NormalSubmissionDraft,
    position: int,
) -> list[discord.SelectOption]:
    selected_entry_id = draft.picks_by_position.get(position)
    start = draft.page * _ENTRY_PAGE_SIZE
    page_entries = list(editor.entries[start : start + _ENTRY_PAGE_SIZE])
    page_entry_ids = {entry.id for entry in page_entries}
    entries_by_id = {entry.id: entry for entry in editor.entries}
    selected_entry = entries_by_id.get(selected_entry_id)
    if selected_entry_id is not None and selected_entry is None:
        raise ValueError("Current Normal pick references an unavailable RaceEntry.")

    options = [
        discord.SelectOption(
            label=CLEAR_PICK_LABEL,
            value=_CLEAR_PICK_VALUE,
            default=selected_entry_id is None,
        )
    ]
    options.extend(
        discord.SelectOption(
            label=_entry_label(gate_number=entry.gate_number, name=entry.name),
            value=str(entry.id),
            default=entry.id == selected_entry_id,
        )
        for entry in page_entries
    )
    if selected_entry is not None and selected_entry.id not in page_entry_ids:
        options.append(
            discord.SelectOption(
                label=_entry_label(
                    gate_number=selected_entry.gate_number,
                    name=selected_entry.name,
                ),
                value=str(selected_entry.id),
                default=True,
                description=CURRENT_SELECTION_DESCRIPTION,
            )
        )
    return options


def format_normal_submission_editor(
    editor: Win5NormalSubmissionEditor,
    draft: Win5NormalSubmissionDraft,
) -> str:
    """Render mention-safe current and unsaved Normal Submission state."""

    return format_normal_submission_editor_copy(
        editor,
        tier=draft.tier,
        picks_by_position=draft.picks_by_position,
        required_positions=_TIER_POSITION_COUNT.get(draft.tier, 0),
        page=draft.page,
        page_count=_page_count(editor),
        notice=draft.notice,
    )


def _special_page_count(editor: Win5SpecialSubmissionEditor) -> int:
    return max(1, (len(editor.races) + _SPECIAL_RACE_PAGE_SIZE - 1) // _SPECIAL_RACE_PAGE_SIZE)


def _special_page_races(
    editor: Win5SpecialSubmissionEditor,
    *,
    page: int,
) -> tuple[Win5RaceCard, ...]:
    start = page * _SPECIAL_RACE_PAGE_SIZE
    return editor.races[start : start + _SPECIAL_RACE_PAGE_SIZE]


def format_special_submission_editor(
    editor: Win5SpecialSubmissionEditor,
    draft: Win5SpecialSubmissionDraft,
) -> str:
    """Render mention-safe current and unsaved Special gate picks."""

    return format_special_submission_editor_copy(
        editor,
        page_races=_special_page_races(editor, page=draft.page),
        picks_by_race_id=draft.picks_by_race_id,
        page=draft.page,
        page_count=_special_page_count(editor),
        notice=draft.notice,
    )


def _interaction_user_id(interaction: object) -> str:
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Discord interaction has no valid user ID.")
    return str(user_id)


def _submission_history_mode_rounds(
    dashboard: Win5MemberSubmissionsDashboard,
    mode: Win5SubmissionsMode,
) -> tuple[Win5MemberSubmissionRoundSummary, ...]:
    if mode == "open":
        return dashboard.open_rounds
    if mode == "scored":
        return dashboard.scored_rounds
    return dashboard.cancelled_rounds


def _submission_history_mode_status(mode: Win5SubmissionsMode) -> Win5RoundStatus:
    if mode == "open":
        return Win5RoundStatus.OPEN
    if mode == "scored":
        return Win5RoundStatus.SCORED
    return Win5RoundStatus.CANCELLED


def format_submission_history_round(page: Win5MemberSubmissionRoundPage) -> str:
    """Render one current-Season owned history card from closed DTO facts."""

    return _format_submission_history_round(
        page,
        tier_position_count=_TIER_POSITION_COUNT,
    )


class Win5SubmissionHistoryRoundSelect(discord.ui.Select):
    """Select one already-loaded Round without retaining a persistence resource."""

    def __init__(self, *, owner: Win5SubmissionsView) -> None:
        self._owner = owner
        rounds = owner.current_rounds
        selected_round_id = owner.selected_round_id
        if selected_round_id is None:
            raise ValueError("A Submission history Select requires one Round.")
        super().__init__(
            custom_id=f"win5-submissions-{owner.mode}-round",
            placeholder=ROUND_SELECT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            row=1,
            options=_submission_history_round_options(
                rounds,
                selected_round_id=selected_round_id,
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            round_id = int(self.values[0])
        except (IndexError, ValueError):
            await self._owner.adapter.send_component_error(
                interaction,
                ROUND_SELECT_ERROR,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return
        await self._owner.select_round(interaction, round_id=round_id)


class Win5SubmissionHistoryPageButton(discord.ui.Button):
    """Load one adjacent bounded detail page through a fresh QueryRunner call."""

    def __init__(
        self,
        *,
        owner: Win5SubmissionsView,
        axis: Literal["submission", "race"],
        direction: Literal[-1, 1],
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._axis = axis
        self._direction = direction
        super().__init__(
            label=history_page_button_label(axis=axis, direction=direction),
            style=discord.ButtonStyle.secondary,
            custom_id=f"win5-submissions-{axis}-{'previous' if direction == -1 else 'next'}",
            row=2 if axis == "submission" else 3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(
            interaction,
            axis=self._axis,
            direction=self._direction,
        )


class Win5SubmissionsView(discord.ui.View):
    """Private active-Season owned history dashboard with no live UoW."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        dashboard: Win5MemberSubmissionsDashboard,
        detail: Win5MemberSubmissionRoundPage | None,
        mode: Win5SubmissionsMode = "open",
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.adapter = adapter
        self.context = context
        self.dashboard = dashboard
        self.mode: Win5SubmissionsMode = "open"
        self.selected_round_id: int | None = None
        self.detail: Win5MemberSubmissionRoundPage | None = None
        self._replacement_started = False
        self._dynamic_items: list[discord.ui.Item[Win5SubmissionsView]] = []
        self._activate(mode, detail=detail)

    def claim_replacement(self) -> bool:
        if self._replacement_started:
            return False
        self._replacement_started = True
        return True

    @property
    def current_rounds(self) -> tuple[Win5MemberSubmissionRoundSummary, ...]:
        return _submission_history_mode_rounds(self.dashboard, self.mode)

    def _activate(
        self,
        mode: Win5SubmissionsMode,
        *,
        detail: Win5MemberSubmissionRoundPage | None,
        dashboard: Win5MemberSubmissionsDashboard | None = None,
    ) -> None:
        candidate_dashboard = dashboard or self.dashboard
        rounds = _submission_history_mode_rounds(candidate_dashboard, mode)
        selected_round_id = detail.round.id if detail is not None else None
        round_ids = {round_.id for round_ in rounds}
        if (bool(rounds) != (detail is not None)) or (
            selected_round_id is not None and selected_round_id not in round_ids
        ):
            raise ValueError("Submission history detail is outside the selected dashboard mode.")
        expected_status = _submission_history_mode_status(mode)
        if detail is not None and detail.round.status != expected_status:
            raise ValueError("Submission history detail status does not match its dashboard mode.")
        self.dashboard = candidate_dashboard
        self.mode = mode
        self.selected_round_id = selected_round_id
        self.detail = detail
        self.open_button.style = discord.ButtonStyle.primary if mode == "open" else discord.ButtonStyle.secondary
        self.scored_button.style = discord.ButtonStyle.primary if mode == "scored" else discord.ButtonStyle.secondary
        self.cancelled_button.style = (
            discord.ButtonStyle.primary if mode == "cancelled" else discord.ButtonStyle.secondary
        )
        self._replace_dynamic_items()

    def _replace_dynamic_items(self) -> None:
        for item in self._dynamic_items:
            self.remove_item(item)
        self._dynamic_items = []
        if self.selected_round_id is not None:
            self._add_dynamic_item(Win5SubmissionHistoryRoundSelect(owner=self))
        detail = self.detail
        if detail is None:
            return
        if detail.total_submission_count > detail.submission_limit:
            self._add_dynamic_item(
                Win5SubmissionHistoryPageButton(
                    owner=self,
                    axis="submission",
                    direction=-1,
                    disabled=detail.submission_offset == 0,
                )
            )
            self._add_dynamic_item(
                Win5SubmissionHistoryPageButton(
                    owner=self,
                    axis="submission",
                    direction=1,
                    disabled=detail.submission_offset + detail.submission_limit >= detail.total_submission_count,
                )
            )
        if detail.round.round_type == Win5RoundType.SPECIAL and detail.total_race_count > detail.race_limit:
            self._add_dynamic_item(
                Win5SubmissionHistoryPageButton(
                    owner=self,
                    axis="race",
                    direction=-1,
                    disabled=detail.race_offset == 0,
                )
            )
            self._add_dynamic_item(
                Win5SubmissionHistoryPageButton(
                    owner=self,
                    axis="race",
                    direction=1,
                    disabled=detail.race_offset + detail.race_limit >= detail.total_race_count,
                )
            )

    def _add_dynamic_item(self, item: discord.ui.Item[Win5SubmissionsView]) -> None:
        self.add_item(item)
        self._dynamic_items.append(item)

    def content(self) -> str:
        if not self.current_rounds:
            return empty_history_message(self.mode)
        if self.detail is None:
            return NO_DETAIL
        return format_submission_history_round(self.detail)

    async def _load_state(
        self,
        interaction: discord.Interaction,
        *,
        mode: Win5SubmissionsMode,
        round_id: int | None,
        submission_offset: int,
        race_offset: int,
    ) -> tuple[Win5MemberSubmissionsDashboard, Win5MemberSubmissionRoundPage | None, bool] | None:
        try:
            dashboard = await self.adapter.blocking_runner(
                lambda: self.adapter.submission_history_queries.get_submissions_dashboard(
                    discord_user_id=str(self.context.user_id),
                )
            )
            rounds = _submission_history_mode_rounds(dashboard, mode)
            current_round_ids = {round_.id for round_ in rounds}
            target_moved = round_id is not None and round_id not in current_round_ids
            if not rounds:
                return dashboard, None, target_moved
            if round_id is None or target_moved:
                target_round_id = rounds[0].id
                submission_offset = 0
                race_offset = 0
            else:
                target_round_id = round_id
            detail = await self.adapter.blocking_runner(
                lambda: self.adapter.submission_history_queries.get_submission_history_round(
                    discord_user_id=str(self.context.user_id),
                    round_id=target_round_id,
                    submission_offset=submission_offset,
                    submission_limit=_SUBMISSION_HISTORY_PAGE_SIZE,
                    race_offset=race_offset,
                    race_limit=_SPECIAL_RACE_PAGE_SIZE,
                )
            )
            expected_status = _submission_history_mode_status(mode)
            if detail.round.status != expected_status:
                await self.adapter.send_component_error(
                    interaction,
                    ROUND_STATUS_CHANGED,
                    command_name=_SUBMISSIONS_COMMAND_NAME,
                )
                return None
            return dashboard, detail, target_moved
        except Win5MemberQueryError:
            await self.adapter.send_component_error(
                interaction,
                HISTORY_REFRESH_ERROR,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
        except Exception:
            self.adapter._log_application_failure(  # noqa: SLF001
                interaction,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            await self.adapter.send_component_error(
                interaction,
                history_refresh_internal_error(correlation_id(interaction)),
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
        return None

    async def _defer_query(self, interaction: discord.Interaction) -> bool:
        try:
            await interaction.response.defer()
        except Exception:
            self.adapter._log_delivery_failure(  # noqa: SLF001
                interaction,
                "submission-dashboard-defer",
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return False
        return True

    async def select_round(
        self,
        interaction: discord.Interaction,
        *,
        round_id: int,
    ) -> None:
        if not await self.adapter._authorize_bound(  # noqa: SLF001
            interaction,
            context=self.context,
            command_name=_SUBMISSIONS_COMMAND_NAME,
        ):
            return
        if not await self._defer_query(interaction):
            return
        loaded = await self._load_state(
            interaction,
            mode=self.mode,
            round_id=round_id,
            submission_offset=0,
            race_offset=0,
        )
        if loaded is None:
            return
        dashboard, detail, target_moved = loaded
        if not await self._replace(
            interaction,
            mode=self.mode,
            dashboard=dashboard,
            detail=detail,
        ):
            return
        if target_moved:
            await self.adapter.send_component_error(
                interaction,
                TARGET_MOVED_NOTICE,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )

    async def _switch(
        self,
        interaction: discord.Interaction,
        *,
        mode: Win5SubmissionsMode,
    ) -> None:
        if not await self.adapter._authorize_bound(  # noqa: SLF001
            interaction,
            context=self.context,
            command_name=_SUBMISSIONS_COMMAND_NAME,
        ):
            return
        if not await self._defer_query(interaction):
            return
        loaded = await self._load_state(
            interaction,
            mode=mode,
            round_id=None,
            submission_offset=0,
            race_offset=0,
        )
        if loaded is None:
            return
        dashboard, detail, _target_moved = loaded
        await self._replace(
            interaction,
            mode=mode,
            dashboard=dashboard,
            detail=detail,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        if not await self.adapter._authorize_bound(  # noqa: SLF001
            interaction,
            context=self.context,
            command_name=_SUBMISSIONS_COMMAND_NAME,
        ):
            return
        current_detail = self.detail
        if not await self._defer_query(interaction):
            return
        loaded = await self._load_state(
            interaction,
            mode=self.mode,
            round_id=current_detail.round.id if current_detail is not None else None,
            submission_offset=current_detail.submission_offset if current_detail is not None else 0,
            race_offset=current_detail.race_offset if current_detail is not None else 0,
        )
        if loaded is None:
            return
        dashboard, detail, target_moved = loaded
        if not await self._replace(
            interaction,
            mode=self.mode,
            dashboard=dashboard,
            detail=detail,
        ):
            return
        if target_moved:
            await self.adapter.send_component_error(
                interaction,
                TARGET_MOVED_NOTICE,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )

    async def change_page(
        self,
        interaction: discord.Interaction,
        *,
        axis: Literal["submission", "race"],
        direction: Literal[-1, 1],
    ) -> None:
        if not await self.adapter._authorize_bound(  # noqa: SLF001
            interaction,
            context=self.context,
            command_name=_SUBMISSIONS_COMMAND_NAME,
        ):
            return
        detail = self.detail
        if detail is None:
            await self.adapter.send_component_error(
                interaction,
                PAGE_UNAVAILABLE,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return
        submission_offset = detail.submission_offset
        race_offset = detail.race_offset
        if axis == "submission":
            submission_offset += direction * detail.submission_limit
            total = detail.total_submission_count
            next_offset = submission_offset
        else:
            race_offset += direction * detail.race_limit
            total = detail.total_race_count
            next_offset = race_offset
        if next_offset < 0 or next_offset >= total:
            await self.adapter.send_component_error(
                interaction,
                PAGE_OUT_OF_RANGE,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return
        if not await self._defer_query(interaction):
            return
        loaded = await self._load_state(
            interaction,
            mode=self.mode,
            round_id=detail.round.id,
            submission_offset=submission_offset,
            race_offset=race_offset,
        )
        if loaded is None:
            return
        dashboard, replacement, target_moved = loaded
        if not await self._replace(
            interaction,
            mode=self.mode,
            dashboard=dashboard,
            detail=replacement,
        ):
            return
        if target_moved:
            await self.adapter.send_component_error(
                interaction,
                TARGET_MOVED_NOTICE,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )

    async def _replace(
        self,
        interaction: discord.Interaction,
        *,
        mode: Win5SubmissionsMode,
        dashboard: Win5MemberSubmissionsDashboard,
        detail: Win5MemberSubmissionRoundPage | None,
    ) -> bool:
        replacement = Win5SubmissionsView(
            adapter=self.adapter,
            context=self.context,
            dashboard=dashboard,
            detail=detail,
            mode=mode,
        )
        if not self.claim_replacement():
            await self.adapter.send_component_error(
                interaction,
                HISTORY_TRANSITION_STARTED,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return False
        self.stop()
        try:
            response = interaction.response
            payload = {
                "content": replacement.content(),
                "allowed_mentions": discord.AllowedMentions.none(),
                "view": replacement,
            }
            if response.is_done():
                await interaction.edit_original_response(**payload)
            else:
                await response.edit_message(**payload)
        except Exception:
            self.adapter._log_delivery_failure(  # noqa: SLF001
                interaction,
                "submission-dashboard-component",
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            await self.adapter.send_component_error(
                interaction,
                HISTORY_TRANSITION_ERROR,
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )
            return False
        return True

    @discord.ui.button(
        label=OPEN_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id="win5-submissions-open",
        row=0,
    )
    async def open_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[Win5SubmissionsView],
    ) -> None:
        await self._switch(interaction, mode="open")

    @discord.ui.button(
        label=SCORED_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-submissions-scored",
        row=0,
    )
    async def scored_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[Win5SubmissionsView],
    ) -> None:
        await self._switch(interaction, mode="scored")

    @discord.ui.button(
        label=CANCELLED_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-submissions-cancelled",
        row=0,
    )
    async def cancelled_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[Win5SubmissionsView],
    ) -> None:
        await self._switch(interaction, mode="cancelled")

    @discord.ui.button(
        label=REFRESH_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id="win5-submissions-refresh",
        row=0,
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[Win5SubmissionsView],
    ) -> None:
        await self.refresh(interaction)


def _terminal_layout(message: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=_COMPONENT_TIMEOUT_SECONDS)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(bounded_discord_message((message,), limit=3500))))
    return view


class Win5SubmissionCancelConfirmButton(discord.ui.Button):
    """Execute an independent versioned Submission cancellation."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        target: Win5CancellableSubmission,
        reason: str | None,
        source_view: Win5SubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._target = target
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=CONFIRM_LABEL,
            custom_id="win5-submission-cancel-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_submission_cancellation(
            interaction,
            context=self._context,
            target=self._target,
            reason=self._reason,
            source_view=self._source_view,
        )


class Win5SubmissionCancelDismissButton(discord.ui.Button):
    """Close an independent cancellation confirmation without writing."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        source_view: Win5SubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=BACK_LABEL,
            custom_id="win5-submission-cancel-dismiss",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.dismiss_submission_cancellation(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class Win5SubmissionCancelConfirmView(discord.ui.LayoutView):
    """Bound confirmation for the independent `/win5 cancel` command."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        target: Win5CancellableSubmission,
        reason: str | None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(format_submission_cancel_confirmation(target, reason=reason))
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5SubmissionCancelConfirmButton(
                adapter=adapter,
                context=context,
                target=target,
                reason=reason,
                source_view=self,
            )
        )
        action_row.add_item(
            Win5SubmissionCancelDismissButton(
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


class Win5NormalSubmissionTierSelect(discord.ui.Select):
    """Static Normal tier selector for one adapter-local draft."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: Win5NormalSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            custom_id="win5-normal-submission-tier",
            placeholder=TIER_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=tier.value,
                    value=tier.value,
                    default=tier == draft.tier,
                )
                for tier in (
                    Win5SubmissionTier.TOP1,
                    Win5SubmissionTier.TOP3,
                    Win5SubmissionTier.TOP5,
                )
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tier = Win5SubmissionTier(self.values[0])
        except (IndexError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                TIER_INVALID,
            )
            return
        await self._adapter.change_normal_submission_tier(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            tier=tier,
            source_view=self._source_view,
        )


class Win5NormalSubmissionPickSelect(discord.ui.Select):
    """One position selector over the current canonical Entry page."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        position: int,
        source_view: Win5NormalSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._position = position
        self._source_view = source_view
        super().__init__(
            custom_id=f"win5-normal-submission-pick-{position}",
            placeholder=entry_placeholder(position),
            min_values=1,
            max_values=1,
            options=_normal_submission_entry_options(
                editor=editor,
                draft=draft,
                position=position,
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            value = self.values[0]
            race_entry_id = None if value == _CLEAR_PICK_VALUE else int(value)
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(
                interaction,
                ENTRY_INVALID,
            )
            return
        await self._adapter.change_normal_submission_pick(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            position=self._position,
            race_entry_id=race_entry_id,
            source_view=self._source_view,
        )


class Win5NormalSubmissionPageButton(discord.ui.Button):
    """Move every position selector to another bounded Entry page."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        page: int,
        label: str,
        source_view: Win5NormalSubmissionEditorView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._page = page
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"win5-normal-submission-page-{page}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.change_normal_submission_page(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            page=self._page,
            source_view=self._source_view,
        )


class Win5NormalSubmissionSaveButton(discord.ui.Button):
    """Persist one non-empty desired Normal Submission state."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: Win5NormalSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.success,
            label=SAVE_LABEL,
            custom_id="win5-normal-submission-save",
            disabled=draft.tier is None or not draft.picks,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.save_normal_submission(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalSubmissionCancelButton(discord.ui.Button):
    """Open explicit accepted-Submission cancellation confirmation."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: Win5NormalSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=CANCEL_LABEL,
            custom_id="win5-normal-submission-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_normal_submission_cancel_confirmation(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalSubmissionEditorView(discord.ui.LayoutView):
    """Prefilled Normal editor for create, supplement, and update semantics."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._mutation_started = False
        page = min(max(draft.page, 0), _page_count(editor) - 1)
        if page != draft.page:
            draft = replace(draft, page=page)
        self.editor = editor
        self.draft = draft

        container = discord.ui.Container(discord.ui.TextDisplay(format_normal_submission_editor(editor, draft)))
        tier_row = discord.ui.ActionRow()
        tier_row.add_item(
            Win5NormalSubmissionTierSelect(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
                source_view=self,
            )
        )
        container.add_item(tier_row)

        position_count = _TIER_POSITION_COUNT.get(draft.tier, 0)
        for position in range(1, position_count + 1):
            row = discord.ui.ActionRow()
            row.add_item(
                Win5NormalSubmissionPickSelect(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    position=position,
                    source_view=self,
                )
            )
            container.add_item(row)

        pages = _page_count(editor)
        if pages > 1:
            page_row = discord.ui.ActionRow()
            page_row.add_item(
                Win5NormalSubmissionPageButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    page=max(0, draft.page - 1),
                    label=PREVIOUS_LABEL,
                    source_view=self,
                    disabled=draft.page == 0,
                )
            )
            page_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=page_label(page=draft.page, page_count=pages),
                    disabled=True,
                )
            )
            page_row.add_item(
                Win5NormalSubmissionPageButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    page=min(pages - 1, draft.page + 1),
                    label=NEXT_LABEL,
                    source_view=self,
                    disabled=draft.page == pages - 1,
                )
            )
            container.add_item(page_row)

        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5NormalSubmissionSaveButton(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
                source_view=self,
            )
        )
        if editor.submission is not None:
            action_row.add_item(
                Win5NormalSubmissionCancelButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    source_view=self,
                )
            )
        container.add_item(action_row)
        self.add_item(container)

    def claim_mutation(self) -> bool:
        if self._mutation_started:
            return False
        self._mutation_started = True
        return True


class Win5NormalSubmissionCancelConfirmButton(discord.ui.Button):
    """Execute explicit cancellation with the editor's exact version."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        source_view: Win5NormalSubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=CONFIRM_LABEL,
            custom_id="win5-normal-submission-cancel-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_normal_submission(
            interaction,
            context=self._context,
            editor=self._editor,
            source_view=self._source_view,
        )


class Win5NormalSubmissionCancelBackButton(discord.ui.Button):
    """Return to the unchanged adapter-local editor draft."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: Win5NormalSubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=BACK_LABEL,
            custom_id="win5-normal-submission-cancel-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.restore_normal_submission_editor(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5NormalSubmissionCancelConfirmView(discord.ui.LayoutView):
    """Explicit cancellation confirmation preserving the accepted row as history."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_editor_cancel_confirmation(
                    round_name=editor.round_name,
                    special=False,
                )
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5NormalSubmissionCancelConfirmButton(
                adapter=adapter,
                context=context,
                editor=editor,
                source_view=self,
            )
        )
        action_row.add_item(
            Win5NormalSubmissionCancelBackButton(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
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


class Win5SpecialSubmissionModal(discord.ui.Modal):
    """TextInput-only gate editor for one bounded Special Race page."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        super().__init__(
            title=special_modal_title(
                page=draft.page,
                page_count=_special_page_count(editor),
            ),
            timeout=_COMPONENT_TIMEOUT_SECONDS,
        )
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        picks_by_race_id = draft.picks_by_race_id
        fields: list[tuple[int, discord.ui.TextInput]] = []
        for race in _special_page_races(editor, page=draft.page):
            gate_number = picks_by_race_id.get(race.id)
            field = discord.ui.TextInput(
                custom_id=f"win5-special-gate-{race.id}",
                label=special_race_input_label(race),
                placeholder=SPECIAL_GATE_PLACEHOLDER,
                default=str(gate_number) if gate_number is not None else None,
                required=False,
                max_length=10,
            )
            self.add_item(field)
            fields.append((race.id, field))
        self.fields = tuple(fields)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.apply_special_submission_page(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            values=tuple((race_id, str(field.value)) for race_id, field in self.fields),
            source_view=self._source_view,
        )


class Win5SpecialSubmissionEditButton(discord.ui.Button):
    """Open the current Special Race page in a TextInput-only Modal."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=SPECIAL_EDIT_LABEL,
            custom_id="win5-special-submission-edit",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_special_submission_modal(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionPageButton(discord.ui.Button):
    """Move the Special editor to another bounded Race page."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        page: int,
        label: str,
        source_view: Win5SpecialSubmissionEditorView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._page = page
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"win5-special-submission-page-{page}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.change_special_submission_page(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            page=self._page,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionSaveButton(discord.ui.Button):
    """Persist one non-empty desired Special Submission state."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.success,
            label=SAVE_LABEL,
            custom_id="win5-special-submission-save",
            disabled=not draft.picks,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.save_special_submission(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionCancelButton(discord.ui.Button):
    """Open explicit accepted Special Submission cancellation confirmation."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=CANCEL_LABEL,
            custom_id="win5-special-submission-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_special_submission_cancel_confirmation(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionEditorView(discord.ui.LayoutView):
    """Prefilled Special editor with Race-page Modal input."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._mutation_started = False
        self.superseded = False
        page = min(max(draft.page, 0), _special_page_count(editor) - 1)
        if page != draft.page:
            draft = replace(draft, page=page)
        self.editor = editor
        self.draft = draft

        container = discord.ui.Container(discord.ui.TextDisplay(format_special_submission_editor(editor, draft)))
        pages = _special_page_count(editor)
        if pages > 1:
            page_row = discord.ui.ActionRow()
            page_row.add_item(
                Win5SpecialSubmissionPageButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    page=max(0, draft.page - 1),
                    label=PREVIOUS_LABEL,
                    source_view=self,
                    disabled=draft.page == 0,
                )
            )
            page_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=page_label(page=draft.page, page_count=pages),
                    disabled=True,
                )
            )
            page_row.add_item(
                Win5SpecialSubmissionPageButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    page=min(pages - 1, draft.page + 1),
                    label=NEXT_LABEL,
                    source_view=self,
                    disabled=draft.page == pages - 1,
                )
            )
            container.add_item(page_row)

        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5SpecialSubmissionEditButton(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
                source_view=self,
            )
        )
        action_row.add_item(
            Win5SpecialSubmissionSaveButton(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
                source_view=self,
            )
        )
        if editor.submission is not None:
            action_row.add_item(
                Win5SpecialSubmissionCancelButton(
                    adapter=adapter,
                    context=context,
                    editor=editor,
                    draft=draft,
                    source_view=self,
                )
            )
        container.add_item(action_row)
        self.add_item(container)

    def mark_superseded(self) -> None:
        """Prevent an already-open Modal from replacing a newer draft View."""

        self.superseded = True
        self.stop()

    def claim_mutation(self) -> bool:
        if self._mutation_started:
            return False
        self._mutation_started = True
        return True


class Win5SpecialSubmissionCancelConfirmButton(discord.ui.Button):
    """Execute explicit Special cancellation with the editor's exact version."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        source_view: Win5SpecialSubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=CONFIRM_LABEL,
            custom_id="win5-special-submission-cancel-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_special_submission(
            interaction,
            context=self._context,
            editor=self._editor,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionCancelBackButton(discord.ui.Button):
    """Return to the unchanged Special adapter-local editor draft."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionCancelConfirmView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._editor = editor
        self._draft = draft
        self._source_view = source_view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=BACK_LABEL,
            custom_id="win5-special-submission-cancel-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.restore_special_submission_editor(
            interaction,
            context=self._context,
            editor=self._editor,
            draft=self._draft,
            source_view=self._source_view,
        )


class Win5SpecialSubmissionCancelConfirmView(discord.ui.LayoutView):
    """Explicit Special cancellation confirmation preserving history."""

    def __init__(
        self,
        *,
        adapter: Win5MemberDiscordAdapter,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._confirmation_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_editor_cancel_confirmation(
                    round_name=editor.round_name,
                    special=True,
                )
            )
        )
        action_row = discord.ui.ActionRow()
        action_row.add_item(
            Win5SpecialSubmissionCancelConfirmButton(
                adapter=adapter,
                context=context,
                editor=editor,
                source_view=self,
            )
        )
        action_row.add_item(
            Win5SpecialSubmissionCancelBackButton(
                adapter=adapter,
                context=context,
                editor=editor,
                draft=draft,
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


def _stop_source_view(source_view: discord.ui.LayoutView) -> None:
    if isinstance(source_view, Win5SpecialSubmissionEditorView):
        source_view.mark_superseded()
        return
    source_view.stop()


@dataclass(frozen=True, slots=True)
class Win5MemberDiscordAdapter:
    """Orchestrate bounded WIN5 member application ports from Discord."""

    queries: Win5MemberQueries
    submission_history_queries: Win5SubmissionHistoryQueries
    commands: Win5MemberCommands
    prepare_command: PrepareDiscordCommand
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_INTERACTION_ERROR,
                command_name=command_name,
            )
            return False
        try:
            return await self.authorize_interaction(interaction, command_name)
        except Exception:
            self._log_application_failure(interaction, command_name=command_name)
            await self.send_component_error(
                interaction,
                authorization_error(correlation_id(interaction)),
                command_name=command_name,
            )
            return False

    async def autocomplete_normal_submission_rounds(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return authorized open Normal Round choices without deferring."""

        try:
            if not await self.authorize_autocomplete(interaction, _SUBMIT_COMMAND_NAME):
                return []
            discord_user_id = _interaction_user_id(interaction)
            choices = await self.blocking_runner(
                lambda: self.queries.search_normal_submission_rounds(
                    discord_user_id=discord_user_id,
                    query=current,
                    limit=25,
                )
            )
        except Win5MemberQueryIdentityError:
            return []
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _SUBMIT_COMMAND_NAME,
            )
            return []
        return _normal_submission_round_choices(choices)

    async def open_normal_submission_editor(
        self,
        interaction: discord.Interaction,
        *,
        round_id: int,
    ) -> None:
        """Authorize and render a prefilled Normal Submission editor."""

        if not await self.prepare_command(
            interaction,
            _SUBMIT_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        try:
            context = Win5MemberInteractionContext.from_interaction(interaction)
            editor = await self.blocking_runner(
                lambda: self.queries.get_normal_submission_editor(
                    discord_user_id=str(context.user_id),
                    round_id=round_id,
                )
            )
            view = Win5NormalSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=Win5NormalSubmissionDraft.from_editor(editor),
            )
        except Win5MemberQueryApprovalPendingError:
            await send_deferred_response_safely(
                interaction,
                _SUBMIT_COMMAND_NAME,
                MEMBER_APPROVAL_PENDING,
            )
            return
        except Win5MemberQueryError as error:
            await send_deferred_response_safely(
                interaction,
                _SUBMIT_COMMAND_NAME,
                _normal_editor_query_error_message(error),
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _SUBMIT_COMMAND_NAME,
            )
            return
        await self._edit_deferred_layout(interaction, view=view)

    async def autocomplete_special_submission_rounds(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return authorized open Special Round choices without deferring."""

        try:
            if not await self.authorize_autocomplete(interaction, _SPECIAL_SUBMIT_COMMAND_NAME):
                return []
            discord_user_id = _interaction_user_id(interaction)
            choices = await self.blocking_runner(
                lambda: self.queries.search_special_submission_rounds(
                    discord_user_id=discord_user_id,
                    query=current,
                    limit=25,
                )
            )
        except Win5MemberQueryIdentityError:
            return []
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return []
        return _special_submission_round_choices(choices)

    async def open_special_submission_editor(
        self,
        interaction: discord.Interaction,
        *,
        round_id: int,
    ) -> None:
        """Authorize and render a prefilled Special Submission editor."""

        if not await self.prepare_command(
            interaction,
            _SPECIAL_SUBMIT_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        try:
            context = Win5MemberInteractionContext.from_interaction(interaction)
            editor = await self.blocking_runner(
                lambda: self.queries.get_special_submission_editor(
                    discord_user_id=str(context.user_id),
                    round_id=round_id,
                )
            )
            view = Win5SpecialSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=Win5SpecialSubmissionDraft.from_editor(editor),
            )
        except Win5MemberQueryApprovalPendingError:
            await send_deferred_response_safely(
                interaction,
                _SPECIAL_SUBMIT_COMMAND_NAME,
                MEMBER_APPROVAL_PENDING,
            )
            return
        except Win5MemberQueryError as error:
            await send_deferred_response_safely(
                interaction,
                _SPECIAL_SUBMIT_COMMAND_NAME,
                _special_editor_query_error_message(error),
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        await self._edit_deferred_layout(
            interaction,
            view=view,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        )

    async def autocomplete_cancellable_submissions(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return owned accepted Submissions in currently open Rounds."""

        try:
            if not await self.authorize_autocomplete(interaction, _CANCEL_COMMAND_NAME):
                return []
            discord_user_id = _interaction_user_id(interaction)
            targets = await self.blocking_runner(
                lambda: self.queries.search_cancellable_submissions(
                    discord_user_id=discord_user_id,
                    query=current,
                    limit=25,
                )
            )
        except Win5MemberQueryIdentityError:
            return []
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _CANCEL_COMMAND_NAME,
            )
            return []
        return _cancellable_submission_choices(targets)

    async def open_submission_cancel_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        submission_id: int,
        reason: str | None,
    ) -> None:
        """Open a bound zero-write cancellation confirmation."""

        if not await self.prepare_command(
            interaction,
            _CANCEL_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        normalized_reason = reason.strip() if reason is not None else None
        normalized_reason = normalized_reason or None
        if normalized_reason is not None and len(normalized_reason) > 255:
            await send_deferred_response_safely(
                interaction,
                _CANCEL_COMMAND_NAME,
                REASON_TOO_LONG,
            )
            return
        try:
            context = Win5MemberInteractionContext.from_interaction(interaction)
            target = await self.blocking_runner(
                lambda: self.queries.get_cancellable_submission(
                    discord_user_id=str(context.user_id),
                    submission_id=submission_id,
                )
            )
            view = Win5SubmissionCancelConfirmView(
                adapter=self,
                context=context,
                target=target,
                reason=normalized_reason,
            )
        except Win5MemberQueryApprovalPendingError:
            await send_deferred_response_safely(
                interaction,
                _CANCEL_COMMAND_NAME,
                MEMBER_APPROVAL_PENDING,
            )
            return
        except Win5MemberQueryError as error:
            await send_deferred_response_safely(
                interaction,
                _CANCEL_COMMAND_NAME,
                _cancellable_query_error_message(error),
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _CANCEL_COMMAND_NAME,
            )
            return
        await self._edit_deferred_layout(
            interaction,
            view=view,
            command_name=_CANCEL_COMMAND_NAME,
        )

    async def dismiss_submission_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Close a cancellation confirmation without a canonical write."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_CANCEL_COMMAND_NAME,
        ):
            return
        await self._replace_component_layout(
            interaction,
            view=_terminal_layout(DISMISSED),
            source_view=source_view,
            command_name=_CANCEL_COMMAND_NAME,
        )

    async def confirm_submission_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        target: Win5CancellableSubmission,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Execute the exact version previewed by the independent cancel command."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_CANCEL_ERROR,
                command_name=_CANCEL_COMMAND_NAME,
            )
            return
        if not await self.prepare_command(
            interaction,
            _CANCEL_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        if not source_view.claim_confirmation():
            await self.send_component_error(
                interaction,
                CANCEL_ALREADY_STARTED,
                command_name=_CANCEL_COMMAND_NAME,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.cancel_submission(
                    CancelWin5Submission(
                        round_id=target.round_id,
                        submission_id=target.submission_id,
                        expected_version=target.version,
                        actor_discord_user_id=str(context.user_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                        reason=reason,
                    )
                )
            )
        except Win5MemberApprovalPendingError:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(MEMBER_APPROVAL_PENDING),
                command_name=_CANCEL_COMMAND_NAME,
            )
        except Win5MemberCommandError as error:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(_independent_cancel_error_message(error)),
                command_name=_CANCEL_COMMAND_NAME,
            )
        except Exception:
            self._log_application_failure(
                interaction,
                command_name=_CANCEL_COMMAND_NAME,
            )
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(cancel_internal_error(correlation_id(interaction))),
                command_name=_CANCEL_COMMAND_NAME,
            )
        else:
            replacement_command = (
                "/win5 submit" if target.round_type == Win5RoundType.NORMAL else "/win5 special-submit"
            )
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(
                    format_cancel_success(
                        round_name=target.round_name,
                        version=result.version,
                        replacement_command=replacement_command,
                    )
                ),
                command_name=_CANCEL_COMMAND_NAME,
            )

    async def change_normal_submission_tier(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        tier: Win5SubmissionTier,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Apply an explicit adapter-local tier change and rebuild position rows."""

        if not await self._authorize_bound(interaction, context=context):
            return
        position_count = _TIER_POSITION_COUNT[tier]
        next_draft = Win5NormalSubmissionDraft(
            tier=tier,
            picks=tuple(pick for pick in draft.picks if pick.position <= position_count),
            page=draft.page,
            notice=(TIER_CHANGE_NOTICE if draft.tier is not None and draft.tier != tier else None),
        )
        await self._replace_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=next_draft,
            source_view=source_view,
        )

    async def change_normal_submission_pick(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        position: int,
        race_entry_id: int | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Apply one adapter-local position selection without writing canonical state."""

        if not await self._authorize_bound(interaction, context=context):
            return
        position_count = _TIER_POSITION_COUNT.get(draft.tier, 0)
        entry_ids = {entry.id for entry in editor.entries}
        if not 1 <= position <= position_count or (race_entry_id is not None and race_entry_id not in entry_ids):
            await self.send_component_error(
                interaction,
                PICK_UNAVAILABLE,
            )
            return
        picks_by_position = draft.picks_by_position
        if race_entry_id is None:
            picks_by_position.pop(position, None)
        else:
            picks_by_position[position] = race_entry_id
        next_draft = Win5NormalSubmissionDraft(
            tier=draft.tier,
            picks=tuple(
                Win5NormalSubmissionPick(
                    position=current_position,
                    race_entry_id=current_entry_id,
                )
                for current_position, current_entry_id in sorted(picks_by_position.items())
            ),
            page=draft.page,
        )
        await self._replace_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=next_draft,
            source_view=source_view,
        )

    async def change_normal_submission_page(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        page: int,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Move the editor's Entry options without changing desired picks."""

        if not await self._authorize_bound(interaction, context=context):
            return
        if not 0 <= page < _page_count(editor):
            await self.send_component_error(
                interaction,
                ENTRY_PAGE_INVALID,
            )
            return
        await self._replace_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=replace(draft, page=page, notice=None),
            source_view=source_view,
        )

    async def show_normal_submission_cancel_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Replace the editor with explicit accepted-Submission cancellation."""

        if not await self._authorize_bound(interaction, context=context):
            return
        if editor.submission is None:
            await self.send_component_error(
                interaction,
                NO_ACCEPTED_SUBMISSION,
            )
            return
        await self._replace_component_layout(
            interaction,
            view=Win5NormalSubmissionCancelConfirmView(
                adapter=self,
                context=context,
                editor=editor,
                draft=draft,
            ),
            source_view=source_view,
        )

    async def restore_normal_submission_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Return from cancellation confirmation without a canonical write."""

        if not await self._authorize_bound(interaction, context=context):
            return
        await self._replace_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )

    async def save_normal_submission(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: Win5NormalSubmissionEditorView,
    ) -> None:
        """Execute one versioned non-empty Normal desired-state save."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_SAVE_ERROR,
            )
            return
        if draft.tier is None or not draft.picks:
            await self.send_component_error(
                interaction,
                EMPTY_SAVE_ERROR,
            )
            return
        if not await self.prepare_command(interaction, _SUBMIT_COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_mutation():
            await self.send_component_error(
                interaction,
                NORMAL_ACTION_ALREADY_STARTED,
            )
            return
        source_view.stop()

        current = editor.submission
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.save_submission(
                    SaveWin5Submission(
                        round_id=editor.round_id,
                        tier=draft.tier,
                        picks=tuple(
                            Win5SubmissionPickInput(
                                race_id=editor.race_id,
                                position=pick.position,
                                race_entry_id=pick.race_entry_id,
                            )
                            for pick in draft.picks
                        ),
                        actor_discord_user_id=str(context.user_id),
                        submission_id=current.id if current is not None else None,
                        expected_version=current.version if current is not None else None,
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
            next_editor = replace(
                editor,
                submission=self._accepted_submission_from_result(result),
            )
            unchanged = current is not None and current.version == result.version
            next_draft = Win5NormalSubmissionDraft(
                tier=result.tier,
                picks=next_editor.submission.picks,
                page=draft.page,
                notice=_committed_submission_receipt_notice(
                    version=result.version,
                    unchanged=unchanged,
                ),
            )
        except Win5MemberApprovalPendingError:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(MEMBER_APPROVAL_PENDING),
            )
        except Win5MemberCommandError as error:
            await self._handle_normal_submission_command_error(
                interaction,
                error=error,
                context=context,
                editor=editor,
                draft=draft,
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(normal_save_internal_error(correlation_id(interaction))),
            )
        else:
            await self._edit_deferred_layout(
                interaction,
                view=Win5NormalSubmissionEditorView(
                    adapter=self,
                    context=context,
                    editor=next_editor,
                    draft=next_draft,
                ),
            )

    async def cancel_normal_submission(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        source_view: Win5NormalSubmissionCancelConfirmView,
    ) -> None:
        """Execute explicit versioned cancellation and preserve history."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_CANCEL_ERROR,
            )
            return
        current = editor.submission
        if current is None:
            await self.send_component_error(interaction, NO_ACCEPTED_SUBMISSION)
            return
        if not await self.prepare_command(interaction, _SUBMIT_COMMAND_NAME, ephemeral=True):
            return
        if not source_view.claim_confirmation():
            await self.send_component_error(
                interaction,
                NORMAL_ACTION_ALREADY_STARTED,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.cancel_submission(
                    CancelWin5Submission(
                        round_id=editor.round_id,
                        submission_id=current.id,
                        expected_version=current.version,
                        actor_discord_user_id=str(context.user_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5MemberApprovalPendingError:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(MEMBER_APPROVAL_PENDING),
            )
        except Win5MemberCommandError as error:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(_normal_cancel_error_message(error)),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(cancel_internal_error(correlation_id(interaction))),
            )
        else:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(
                    format_cancel_success(
                        round_name=editor.round_name,
                        version=result.version,
                        replacement_command="/win5 submit",
                        fixed_normal_replacement=True,
                    )
                ),
            )

    async def change_special_submission_page(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        page: int,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Move the Special editor without changing desired gate picks."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        ):
            return
        if not 0 <= page < _special_page_count(editor):
            await self.send_component_error(
                interaction,
                SPECIAL_PAGE_INVALID,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        await self._replace_special_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=replace(draft, page=page, notice=None),
            source_view=source_view,
        )

    async def open_special_submission_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        """Open the current Special Race page without a canonical write."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        ):
            return
        try:
            await interaction.response.send_modal(
                Win5SpecialSubmissionModal(
                    adapter=self,
                    context=context,
                    editor=editor,
                    draft=draft,
                    source_view=source_view,
                )
            )
        except Exception:
            self._log_delivery_failure(
                interaction,
                "special-submission-modal",
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )

    async def apply_special_submission_page(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        values: tuple[tuple[int, str], ...],
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Apply one Modal page to adapter-local desired state without writing."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        ):
            return
        if isinstance(source_view, Win5SpecialSubmissionEditorView) and source_view.superseded:
            await self.send_component_error(
                interaction,
                SPECIAL_MODAL_STALE,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        expected_race_ids = tuple(race.id for race in _special_page_races(editor, page=draft.page))
        if tuple(race_id for race_id, _ in values) != expected_race_ids:
            await self.send_component_error(
                interaction,
                SPECIAL_MODAL_TARGET_MISMATCH,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return

        picks_by_race_id = draft.picks_by_race_id
        for race_id, raw_value in values:
            value = raw_value.strip()
            if not value:
                picks_by_race_id.pop(race_id, None)
                continue
            if not value.isdecimal():
                await self.send_component_error(
                    interaction,
                    SPECIAL_GATE_INTEGER_ERROR,
                    command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
                )
                return
            gate_number = int(value)
            if not 1 <= gate_number <= _MAX_GATE_NUMBER:
                await self.send_component_error(
                    interaction,
                    SPECIAL_GATE_RANGE_ERROR,
                    command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
                )
                return
            picks_by_race_id[race_id] = gate_number

        next_draft = Win5SpecialSubmissionDraft(
            picks=tuple(
                Win5SpecialSubmissionPick(
                    race_id=race.id,
                    gate_number=picks_by_race_id[race.id],
                )
                for race in editor.races
                if race.id in picks_by_race_id
            ),
            page=draft.page,
            notice=SPECIAL_PAGE_APPLIED_NOTICE,
        )
        await self._replace_special_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=next_draft,
            source_view=source_view,
        )

    async def show_special_submission_cancel_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Replace the Special editor with explicit cancellation confirmation."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        ):
            return
        if editor.submission is None:
            await self.send_component_error(
                interaction,
                NO_ACCEPTED_SUBMISSION,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        await self._replace_component_layout(
            interaction,
            view=Win5SpecialSubmissionCancelConfirmView(
                adapter=self,
                context=context,
                editor=editor,
                draft=draft,
            ),
            source_view=source_view,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        )

    async def restore_special_submission_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        """Return from Special cancellation confirmation without writing."""

        if not await self._authorize_bound(
            interaction,
            context=context,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        ):
            return
        await self._replace_special_editor_view(
            interaction,
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )

    async def save_special_submission(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: Win5SpecialSubmissionEditorView,
    ) -> None:
        """Execute one versioned non-empty Special desired-state save."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_SAVE_ERROR,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        if not draft.picks:
            await self.send_component_error(
                interaction,
                EMPTY_SAVE_ERROR,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        if not await self.prepare_command(
            interaction,
            _SPECIAL_SUBMIT_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        if not source_view.claim_mutation():
            await self.send_component_error(
                interaction,
                SPECIAL_ACTION_ALREADY_STARTED,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        _stop_source_view(source_view)

        current = editor.submission
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.save_submission(
                    SaveWin5Submission(
                        round_id=editor.round_id,
                        tier=Win5SubmissionTier.SPECIAL_WINNER,
                        picks=tuple(
                            Win5SubmissionPickInput(
                                race_id=pick.race_id,
                                position=1,
                                gate_number=pick.gate_number,
                            )
                            for pick in draft.picks
                        ),
                        actor_discord_user_id=str(context.user_id),
                        submission_id=current.id if current is not None else None,
                        expected_version=current.version if current is not None else None,
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
            next_editor = replace(
                editor,
                submission=self._accepted_special_submission_from_result(result),
            )
            unchanged = current is not None and current.version == result.version
            next_draft = Win5SpecialSubmissionDraft(
                picks=next_editor.submission.picks,
                page=draft.page,
                notice=_committed_submission_receipt_notice(
                    version=result.version,
                    unchanged=unchanged,
                ),
            )
        except Win5MemberApprovalPendingError:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(MEMBER_APPROVAL_PENDING),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
        except Win5MemberCommandError as error:
            await self._handle_special_submission_command_error(
                interaction,
                error=error,
                context=context,
                editor=editor,
                draft=draft,
            )
        except Exception:
            self._log_application_failure(
                interaction,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(special_save_internal_error(correlation_id(interaction))),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
        else:
            await self._edit_deferred_layout(
                interaction,
                view=Win5SpecialSubmissionEditorView(
                    adapter=self,
                    context=context,
                    editor=next_editor,
                    draft=next_draft,
                ),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )

    async def cancel_special_submission(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        source_view: Win5SpecialSubmissionCancelConfirmView,
    ) -> None:
        """Execute explicit versioned Special cancellation and preserve history."""

        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_CANCEL_ERROR,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        current = editor.submission
        if current is None:
            await self.send_component_error(
                interaction,
                NO_ACCEPTED_SUBMISSION,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        if not await self.prepare_command(
            interaction,
            _SPECIAL_SUBMIT_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        if not source_view.claim_confirmation():
            await self.send_component_error(
                interaction,
                SPECIAL_ACTION_ALREADY_STARTED,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.cancel_submission(
                    CancelWin5Submission(
                        round_id=editor.round_id,
                        submission_id=current.id,
                        expected_version=current.version,
                        actor_discord_user_id=str(context.user_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                    )
                )
            )
        except Win5MemberApprovalPendingError:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(MEMBER_APPROVAL_PENDING),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
        except Win5MemberCommandError as error:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(_special_cancel_error_message(error)),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
        except Exception:
            self._log_application_failure(
                interaction,
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(special_cancel_internal_error(correlation_id(interaction))),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )
        else:
            await self._edit_deferred_layout(
                interaction,
                view=_terminal_layout(
                    format_cancel_success(
                        round_name=editor.round_name,
                        version=result.version,
                        replacement_command="/win5 special-submit",
                        special_heading=True,
                    )
                ),
                command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
            )

    async def _replace_special_editor_view(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        await self._replace_component_layout(
            interaction,
            view=Win5SpecialSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=draft,
            ),
            source_view=source_view,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        )

    async def _replace_editor_view(
        self,
        interaction: discord.Interaction,
        *,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
        source_view: discord.ui.LayoutView,
    ) -> None:
        await self._replace_component_layout(
            interaction,
            view=Win5NormalSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=draft,
            ),
            source_view=source_view,
        )

    async def _replace_component_layout(
        self,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        source_view: discord.ui.LayoutView,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> None:
        _stop_source_view(source_view)
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(
                interaction,
                "editor-transition",
                command_name=command_name,
            )
            await self.send_component_error(
                interaction,
                editor_transition_error(command_name=command_name),
                command_name=command_name,
            )

    async def _edit_deferred_layout(
        self,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(
                interaction,
                "editor-response",
                command_name=command_name,
            )

    async def send_component_error(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> None:
        """Send one fixed private component error without exposing internals."""

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
            self._log_delivery_failure(
                interaction,
                "component-error",
                command_name=command_name,
            )

    async def _handle_normal_submission_command_error(
        self,
        interaction: discord.Interaction,
        *,
        error: Win5MemberCommandError,
        context: Win5MemberInteractionContext,
        editor: Win5NormalSubmissionEditor,
        draft: Win5NormalSubmissionDraft,
    ) -> None:
        if isinstance(error, Win5SubmissionVersionConflictError):
            view = _terminal_layout(NORMAL_SAVE_STALE)
        elif isinstance(error, (Win5RoundUnavailableError, Win5MemberIdentityError)):
            view = _terminal_layout(SAVE_UNAVAILABLE)
        else:
            if isinstance(error, Win5SubmissionInvalidError):
                detail = NORMAL_INVALID_DETAIL
            elif isinstance(error, Win5EmptyPickSetUnsupportedError):
                detail = NORMAL_EMPTY_DETAIL
            elif isinstance(error, Win5SubmissionUnavailableError):
                detail = SUBMISSION_UNAVAILABLE_DETAIL
            else:
                detail = CURRENT_DRAFT_UNAVAILABLE_DETAIL
            view = Win5NormalSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=replace(draft, notice=save_notice(detail)),
            )
        await self._edit_deferred_layout(interaction, view=view)

    async def _handle_special_submission_command_error(
        self,
        interaction: discord.Interaction,
        *,
        error: Win5MemberCommandError,
        context: Win5MemberInteractionContext,
        editor: Win5SpecialSubmissionEditor,
        draft: Win5SpecialSubmissionDraft,
    ) -> None:
        if isinstance(error, Win5SubmissionVersionConflictError):
            view = _terminal_layout(SPECIAL_SAVE_STALE)
        elif isinstance(error, (Win5RoundUnavailableError, Win5MemberIdentityError)):
            view = _terminal_layout(SAVE_UNAVAILABLE)
        else:
            if isinstance(error, Win5SubmissionInvalidError):
                detail = SPECIAL_INVALID_DETAIL
            elif isinstance(error, Win5EmptyPickSetUnsupportedError):
                detail = SPECIAL_EMPTY_DETAIL
            elif isinstance(error, Win5SubmissionUnavailableError):
                detail = SUBMISSION_UNAVAILABLE_DETAIL
            else:
                detail = CURRENT_DRAFT_UNAVAILABLE_DETAIL
            view = Win5SpecialSubmissionEditorView(
                adapter=self,
                context=context,
                editor=editor,
                draft=replace(draft, notice=save_notice(detail)),
            )
        await self._edit_deferred_layout(
            interaction,
            view=view,
            command_name=_SPECIAL_SUBMIT_COMMAND_NAME,
        )

    @staticmethod
    def _accepted_submission_from_result(
        result: SavedWin5Submission,
    ) -> Win5AcceptedNormalSubmission:
        picks: list[Win5NormalSubmissionPick] = []
        for pick in result.picks:
            if pick.race_entry_id is None or pick.gate_number is not None:
                raise ValueError("Saved Normal Submission returned a non-Normal pick payload.")
            picks.append(
                Win5NormalSubmissionPick(
                    position=pick.position,
                    race_entry_id=pick.race_entry_id,
                )
            )
        return Win5AcceptedNormalSubmission(
            id=result.submission_id,
            tier=result.tier,
            version=result.version,
            picks=tuple(picks),
        )

    @staticmethod
    def _accepted_special_submission_from_result(
        result: SavedWin5Submission,
    ) -> Win5AcceptedSpecialSubmission:
        if result.tier != Win5SubmissionTier.SPECIAL_WINNER:
            raise ValueError("Saved Special Submission returned a non-Special tier.")
        picks: list[Win5SpecialSubmissionPick] = []
        seen_race_ids: set[int] = set()
        for pick in result.picks:
            if (
                pick.race_entry_id is not None
                or pick.gate_number is None
                or pick.position != 1
                or pick.race_id in seen_race_ids
            ):
                raise ValueError("Saved Special Submission returned an invalid gate pick payload.")
            seen_race_ids.add(pick.race_id)
            picks.append(
                Win5SpecialSubmissionPick(
                    race_id=pick.race_id,
                    gate_number=pick.gate_number,
                )
            )
        return Win5AcceptedSpecialSubmission(
            id=result.submission_id,
            version=result.version,
            picks=tuple(picks),
        )

    @staticmethod
    def _log_application_failure(
        interaction: object,
        *,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            command_name,
        )

    @staticmethod
    def _log_delivery_failure(
        interaction: object,
        stage: str,
        *,
        command_name: str = _SUBMIT_COMMAND_NAME,
    ) -> None:
        logger.error(
            "Discord response delivery failed correlation_id=%s command=%s stage=%s",
            correlation_id(interaction),
            command_name,
            stage,
        )

    async def show_active_season_info(self, interaction: discord.Interaction) -> None:
        """Authorize and render the private active-Season member summary."""

        if not await self.prepare_command(
            interaction,
            _INFO_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        try:
            discord_user_id = _interaction_user_id(interaction)
            info = await self.blocking_runner(
                lambda: self.queries.get_active_season_info(discord_user_id=discord_user_id)
            )
        except Win5MemberQueryError as error:
            await send_deferred_response_safely(
                interaction,
                _INFO_COMMAND_NAME,
                _member_info_error_message(error),
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(interaction, _INFO_COMMAND_NAME)
            return
        await send_deferred_response_safely(
            interaction,
            _INFO_COMMAND_NAME,
            format_active_season_info(info),
        )

    async def list_open_rounds(self, interaction: discord.Interaction) -> None:
        """Authorize, query, and publish the current open WIN5 Round list."""

        if not await self.prepare_command(
            interaction,
            _ROUNDS_COMMAND_NAME,
            ephemeral=False,
        ):
            return
        try:
            rounds = await self.blocking_runner(lambda: self.queries.list_open_rounds(limit=100))
        except Exception:
            await send_private_error_after_defer_safely(interaction, _ROUNDS_COMMAND_NAME)
            return
        await send_deferred_response_safely(
            interaction,
            _ROUNDS_COMMAND_NAME,
            format_open_rounds(rounds),
        )

    async def show_submissions(self, interaction: discord.Interaction) -> None:
        """Authorize and render the private current-Season owned history View."""

        if not await self.prepare_command(
            interaction,
            _SUBMISSIONS_COMMAND_NAME,
            ephemeral=True,
        ):
            return
        try:
            context = Win5MemberInteractionContext.from_interaction(interaction)
            discord_user_id = _interaction_user_id(interaction)
            dashboard = await self.blocking_runner(
                lambda: self.submission_history_queries.get_submissions_dashboard(
                    discord_user_id=discord_user_id,
                )
            )
            detail = None
            if dashboard.open_rounds:
                detail = await self.blocking_runner(
                    lambda: self.submission_history_queries.get_submission_history_round(
                        discord_user_id=discord_user_id,
                        round_id=dashboard.open_rounds[0].id,
                        submission_offset=0,
                        submission_limit=_SUBMISSION_HISTORY_PAGE_SIZE,
                        race_offset=0,
                        race_limit=_SPECIAL_RACE_PAGE_SIZE,
                    )
                )
                if detail.round.status != Win5RoundStatus.OPEN:
                    await send_deferred_response_safely(
                        interaction,
                        _SUBMISSIONS_COMMAND_NAME,
                        INITIAL_STATUS_CHANGED,
                    )
                    return
            view = Win5SubmissionsView(
                adapter=self,
                context=context,
                dashboard=dashboard,
                detail=detail,
            )
        except Win5MemberQueryError as error:
            await send_deferred_response_safely(
                interaction,
                _SUBMISSIONS_COMMAND_NAME,
                _submissions_query_error_message(error),
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _SUBMISSIONS_COMMAND_NAME,
            )
            return
        try:
            await interaction.edit_original_response(
                content=view.content(),
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_delivery_failure(
                interaction,
                "submission-dashboard-response",
                command_name=_SUBMISSIONS_COMMAND_NAME,
            )

    async def autocomplete_standings_seasons(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return authorized active/closed Season choices without deferring."""

        try:
            if not await self.authorize_autocomplete(interaction, _STANDINGS_COMMAND_NAME):
                return []
            seasons = await self.blocking_runner(lambda: self.queries.search_standings_seasons(query=current, limit=25))
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _STANDINGS_COMMAND_NAME,
            )
            return []
        return _standings_season_choices(seasons)

    async def show_standings(
        self,
        interaction: discord.Interaction,
        *,
        season_id: int,
        ranking: Literal["season", "top1"],
    ) -> None:
        """Authorize, query, and publish one active/closed Season standings view."""

        if not await self.prepare_command(
            interaction,
            _STANDINGS_COMMAND_NAME,
            ephemeral=False,
        ):
            return
        try:
            standings = await self.blocking_runner(
                lambda: self.queries.get_standings(
                    season_id=season_id,
                    ranking=ranking,
                    limit=100,
                )
            )
        except Win5StandingsSeasonUnavailableError:
            await send_private_response_after_public_defer_safely(
                interaction,
                _STANDINGS_COMMAND_NAME,
                STANDINGS_UNAVAILABLE,
            )
            return
        except Exception:
            await send_private_error_after_defer_safely(interaction, _STANDINGS_COMMAND_NAME)
            return
        await send_deferred_response_safely(
            interaction,
            _STANDINGS_COMMAND_NAME,
            format_standings(standings),
        )


class Win5MemberCommandGroup(app_commands.Group):
    """Incrementally registered `/win5` member command group."""

    def __init__(self, *, adapter: Win5MemberDiscordAdapter) -> None:
        super().__init__(name="win5", description=ROOT_DESCRIPTION)
        self._adapter = adapter

    @app_commands.command(name="info", description=INFO_DESCRIPTION)
    async def info(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_active_season_info(interaction)

    @app_commands.command(name="rounds", description=ROUNDS_DESCRIPTION)
    async def rounds(self, interaction: discord.Interaction) -> None:
        await self._adapter.list_open_rounds(interaction)

    async def _normal_submission_round_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_normal_submission_rounds(
            interaction,
            current,
        )

    @app_commands.command(name="submit", description=SUBMIT_DESCRIPTION)
    @app_commands.rename(round_id=ROUND_ID_OPTION_NAME)
    @app_commands.describe(round_id=SUBMIT_ROUND_DESCRIPTION)
    @app_commands.autocomplete(round_id=_normal_submission_round_autocomplete)
    async def submit(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        await self._adapter.open_normal_submission_editor(
            interaction,
            round_id=round_id,
        )

    async def _special_submission_round_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_special_submission_rounds(
            interaction,
            current,
        )

    @app_commands.command(name="special-submit", description=SPECIAL_SUBMIT_DESCRIPTION)
    @app_commands.rename(round_id=ROUND_ID_OPTION_NAME)
    @app_commands.describe(round_id=SPECIAL_SUBMIT_ROUND_DESCRIPTION)
    @app_commands.autocomplete(round_id=_special_submission_round_autocomplete)
    async def special_submit(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        await self._adapter.open_special_submission_editor(
            interaction,
            round_id=round_id,
        )

    @app_commands.command(name="submissions", description=SUBMISSIONS_DESCRIPTION)
    async def submissions(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_submissions(interaction)

    async def _cancellable_submission_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_cancellable_submissions(
            interaction,
            current,
        )

    @app_commands.command(name="cancel", description=CANCEL_DESCRIPTION)
    @app_commands.rename(
        submission_id=SUBMISSION_ID_OPTION_NAME,
        reason=REASON_OPTION_NAME,
    )
    @app_commands.describe(
        submission_id=CANCEL_SUBMISSION_DESCRIPTION,
        reason=CANCEL_REASON_DESCRIPTION,
    )
    @app_commands.autocomplete(submission_id=_cancellable_submission_autocomplete)
    async def cancel(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        reason: str | None = None,
    ) -> None:
        await self._adapter.open_submission_cancel_confirmation(
            interaction,
            submission_id=submission_id,
            reason=reason,
        )

    async def _standings_season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_standings_seasons(interaction, current)

    @app_commands.command(name="standings", description=STANDINGS_DESCRIPTION)
    @app_commands.rename(
        season_id=SEASON_ID_OPTION_NAME,
        ranking=RANKING_OPTION_NAME,
    )
    @app_commands.describe(
        season_id=STANDINGS_SEASON_DESCRIPTION,
        ranking=STANDINGS_RANKING_DESCRIPTION,
    )
    @app_commands.autocomplete(season_id=_standings_season_autocomplete)
    async def standings(
        self,
        interaction: discord.Interaction,
        season_id: int,
        ranking: Literal["season", "top1"] = "season",
    ) -> None:
        await self._adapter.show_standings(
            interaction,
            season_id=season_id,
            ranking=ranking,
        )
