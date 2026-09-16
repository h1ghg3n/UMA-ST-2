"""Discord adapter for native Match pending result submission."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultSubmissionAuditError,
    MatchResultSubmissionCommands,
    MatchResultSubmissionEntry,
    MatchResultSubmissionError,
    MatchResultSubmissionIdempotencyConflictError,
    MatchResultSubmissionInvalidSourceError,
    MatchResultSubmissionReasonRequiredError,
    MatchResultSubmissionStaleError,
    MatchResultSubmissionTarget,
    MatchResultSubmissionUnavailableError,
    MatchStaffResultSubmissionQueries,
    MatchStaffResultSubmissionQueryError,
    SaveMatchResultSubmission,
)
from uma_st2.domain.match import MatchResultSourceKind, MatchStatus

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_result_submission import (
    BOARD_INCOMPLETE,
    BOUND_INTERACTION_ERROR,
    BOUND_SUBMIT_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CORRECTION_PLACEHOLDER,
    CORRECTION_REASON_REQUIRED,
    CORRECTION_REQUIRES_COMPLETE,
    ENTRY_ALREADY_ASSIGNED,
    ENTRY_NUMBER_ASCII_ERROR,
    ENTRY_NUMBER_LABEL,
    ENTRY_NUMBER_UNAVAILABLE,
    ENTRY_UNAVAILABLE,
    FINISH_TIME_FORMAT_ERROR,
    FINISH_TIME_POSITIVE_ERROR,
    FINISH_TIME_TYPE_ERROR,
    IDEMPOTENCY_CONFLICT,
    MARGIN_LABEL,
    MARGIN_TOO_LONG,
    NEXT_LABEL,
    POPULARITY_ASCII_ERROR,
    POPULARITY_LABEL,
    POPULARITY_RANGE_ERROR,
    PREVIOUS_LABEL,
    RANK_UNAVAILABLE,
    REASON_REQUIRED,
    REASON_TOO_LONG,
    RESET_LABEL,
    REVIEW_REQUIRED,
    STALE,
    SUBMISSION_STARTED,
    SUBMIT_LABEL,
    UNAVAILABLE,
    WINNER_MARGIN_ERROR,
    WINNER_TIME_LABEL,
    ResultSubmissionEditorEntry,
    assignment_choice_name,
    assignment_placeholder,
    correction_choice_name,
    correction_modal_title,
    evidence_error,
    format_match_finish_time,
    format_match_result_submission_success,
    match_result_submission_autocomplete_choices,
    open_error,
    open_internal_error,
    submission_error,
    submission_internal_error,
)
from .strings.match_staff_result_submission import (
    format_match_result_submission_editor as _format_match_result_submission_editor,
)
from .strings.match_staff_workflows import RESULT_TRANSITION_ERROR

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.result-submit"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_ENTRY_PAGE_SIZE = 8
_FINISH_TIME_PATTERN = re.compile(
    r"(?:(?P<minutes>[0-9]+):(?P<seconds>[0-5][0-9])|(?P<total>[0-9]+))\.(?P<tenth>[0-9])"
)


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def parse_match_finish_time(value: str) -> int | None:
    """Parse optional M:SS.d or total S.d input into exact milliseconds."""

    if not isinstance(value, str):
        raise ValueError(FINISH_TIME_TYPE_ERROR)
    normalized = value.strip()
    if not normalized:
        return None
    match = _FINISH_TIME_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError(FINISH_TIME_FORMAT_ERROR)
    if match.group("total") is not None:
        total_seconds = int(match.group("total"))
    else:
        total_seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
    milliseconds = total_seconds * 1000 + int(match.group("tenth")) * 100
    if milliseconds <= 0:
        raise ValueError(FINISH_TIME_POSITIVE_ERROR)
    return milliseconds


@dataclass(frozen=True, slots=True)
class MatchResultDraftRow:
    """One adapter-local rank assignment and optional source details."""

    rank: int
    entry_id: int
    entry_number: int
    game_account_name: str
    horse_name: str
    popularity_rank: int | None = None
    margin: str | None = None

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.rank, self.entry_id, self.entry_number)
        ):
            raise ValueError("rank and Entry identity must be positive integers.")
        if self.popularity_rank is not None and (
            isinstance(self.popularity_rank, bool)
            or not isinstance(self.popularity_rank, int)
            or self.popularity_rank <= 0
        ):
            raise ValueError("popularity_rank must be a positive integer or None.")
        margin = self.margin.strip() if isinstance(self.margin, str) else self.margin
        if margin == "":
            margin = None
        if margin is not None and (not isinstance(margin, str) or len(margin) > 16):
            raise ValueError(MARGIN_TOO_LONG)
        if self.rank == 1 and margin is not None:
            raise ValueError(WINNER_MARGIN_ERROR)
        object.__setattr__(self, "margin", margin)

    def candidate_entry(self) -> MatchResultCandidateEntry:
        return MatchResultCandidateEntry(
            entry_id=self.entry_id,
            entry_number=self.entry_number,
            rank=self.rank,
            popularity_rank=self.popularity_rank,
            margin=self.margin,
        )


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionDraft:
    """Complete or partial adapter-local rank board with no persistence resource."""

    target: MatchResultSubmissionTarget
    rows: tuple[MatchResultDraftRow, ...] = ()
    finish_time_ms: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, MatchResultSubmissionTarget):
            raise ValueError("target must be MatchResultSubmissionTarget.")
        rows = tuple(sorted(self.rows, key=lambda row: row.rank))
        if any(not isinstance(row, MatchResultDraftRow) for row in rows):
            raise ValueError("rows must contain MatchResultDraftRow values.")
        if tuple(row.rank for row in rows) != tuple(range(1, len(rows) + 1)):
            raise ValueError("Draft ranks must be contiguous from 1.")
        if len({row.entry_id for row in rows}) != len(rows):
            raise ValueError("Draft Entry assignments must be unique.")
        entry_ids = {entry.entry_id for entry in self.target.entries}
        if any(row.entry_id not in entry_ids for row in rows):
            raise ValueError("Draft contains an Entry outside the target Match.")
        if len(rows) > len(self.target.entries):
            raise ValueError("Draft cannot exceed target field size.")
        if self.finish_time_ms is not None and (
            isinstance(self.finish_time_ms, bool)
            or not isinstance(self.finish_time_ms, int)
            or self.finish_time_ms <= 0
            or self.finish_time_ms % 100 != 0
        ):
            raise ValueError("finish_time_ms must be a positive exact 0.1-second value.")
        reason = self.reason.strip() if isinstance(self.reason, str) else self.reason
        if reason == "":
            reason = None
        if reason is not None and (not isinstance(reason, str) or len(reason) > 255):
            raise ValueError(REASON_TOO_LONG)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "reason", reason)

    @classmethod
    def from_target(
        cls,
        target: MatchResultSubmissionTarget,
        *,
        reason: str | None,
    ) -> MatchResultSubmissionDraft:
        candidate = target.preferred_draft_candidate
        if candidate is None:
            return cls(target=target, reason=reason)
        entries_by_id = {entry.entry_id: entry for entry in target.entries}
        rows = tuple(
            MatchResultDraftRow(
                rank=item.rank,
                entry_id=item.entry_id,
                entry_number=item.entry_number,
                game_account_name=entries_by_id[item.entry_id].game_account_name,
                horse_name=entries_by_id[item.entry_id].horse_name,
                popularity_rank=item.popularity_rank,
                margin=item.margin,
            )
            for item in candidate.entries
        )
        return cls(
            target=target,
            rows=rows,
            finish_time_ms=candidate.finish_time_ms,
            reason=reason,
        )

    @property
    def is_complete(self) -> bool:
        return len(self.rows) == len(self.target.entries)

    def assign(self, *, entry_id: int) -> MatchResultSubmissionDraft:
        if self.is_complete or entry_id in {row.entry_id for row in self.rows}:
            raise ValueError(ENTRY_ALREADY_ASSIGNED)
        entry = next((entry for entry in self.target.entries if entry.entry_id == entry_id), None)
        if entry is None:
            raise ValueError(ENTRY_UNAVAILABLE)
        return replace(
            self,
            rows=(
                *self.rows,
                MatchResultDraftRow(
                    rank=len(self.rows) + 1,
                    entry_id=entry.entry_id,
                    entry_number=entry.entry_number,
                    game_account_name=entry.game_account_name,
                    horse_name=entry.horse_name,
                ),
            ),
        )

    def correct(
        self,
        *,
        rank: int,
        entry_number: int,
        popularity_rank: int | None,
        detail: str,
    ) -> MatchResultSubmissionDraft:
        if not self.is_complete:
            raise ValueError(CORRECTION_REQUIRES_COMPLETE)
        selected_index = rank - 1
        if not 0 <= selected_index < len(self.rows):
            raise ValueError(RANK_UNAVAILABLE)
        new_entry = next(
            (entry for entry in self.target.entries if entry.entry_number == entry_number),
            None,
        )
        if new_entry is None:
            raise ValueError(ENTRY_NUMBER_UNAVAILABLE)
        if popularity_rank is not None and not 1 <= popularity_rank <= len(self.target.entries):
            raise ValueError(POPULARITY_RANGE_ERROR)
        rows = list(self.rows)
        selected = rows[selected_index]
        other_index = next(
            (index for index, row in enumerate(rows) if row.entry_id == new_entry.entry_id),
            None,
        )
        if other_index is not None and other_index != selected_index:
            other = rows[other_index]
            rows[other_index] = replace(
                other,
                entry_id=selected.entry_id,
                entry_number=selected.entry_number,
                game_account_name=selected.game_account_name,
                horse_name=selected.horse_name,
                popularity_rank=selected.popularity_rank,
            )
        margin = detail if rank >= 2 else None
        finish_time_ms = self.finish_time_ms if rank >= 2 else parse_match_finish_time(detail)
        rows[selected_index] = replace(
            selected,
            entry_id=new_entry.entry_id,
            entry_number=new_entry.entry_number,
            game_account_name=new_entry.game_account_name,
            horse_name=new_entry.horse_name,
            popularity_rank=popularity_rank,
            margin=margin,
        )
        candidate = MatchResultCandidate(
            entries=tuple(row.candidate_entry() for row in rows),
            finish_time_ms=finish_time_ms,
        )
        return replace(self, rows=tuple(rows), finish_time_ms=candidate.finish_time_ms)

    def candidate(self) -> MatchResultCandidate:
        if not self.is_complete:
            raise ValueError(BOARD_INCOMPLETE)
        return MatchResultCandidate(
            entries=tuple(row.candidate_entry() for row in self.rows),
            finish_time_ms=self.finish_time_ms,
        )


def _page_count(draft: MatchResultSubmissionDraft) -> int:
    return max(1, (len(draft.target.entries) + _ENTRY_PAGE_SIZE - 1) // _ENTRY_PAGE_SIZE)


def _page_entries(
    draft: MatchResultSubmissionDraft,
    *,
    page_index: int,
) -> tuple[MatchResultSubmissionEntry, ...]:
    start = page_index * _ENTRY_PAGE_SIZE
    return draft.target.entries[start : start + _ENTRY_PAGE_SIZE]


def format_match_result_submission_editor(
    draft: MatchResultSubmissionDraft,
    *,
    page_index: int,
    seen_pages: frozenset[int],
) -> str:
    """Render one bounded private editor page from adapter-local facts."""

    rows_by_entry = {row.entry_id: row for row in draft.rows}
    entries = tuple(
        ResultSubmissionEditorEntry(
            entry_number=entry.entry_number,
            game_account_name=entry.game_account_name,
            horse_name=entry.horse_name,
            rank=row.rank if (row := rows_by_entry.get(entry.entry_id)) is not None else None,
            popularity_rank=row.popularity_rank if row is not None else None,
            finish_time_ms=draft.finish_time_ms if row is not None and row.rank == 1 else None,
            margin=row.margin if row is not None else None,
        )
        for entry in _page_entries(draft, page_index=page_index)
    )
    pages = _page_count(draft)
    return _format_match_result_submission_editor(
        match_name=draft.target.match_name,
        status=draft.target.status.value,
        next_revision_number=draft.target.next_revision_number,
        assigned_count=len(draft.rows),
        total_count=len(draft.target.entries),
        page_index=page_index,
        page_count=pages,
        entries=entries,
        is_complete=draft.is_complete,
        unseen_page_count=pages - len(seen_pages),
        reason=draft.reason,
    )


class MatchResultAssignmentSelect(discord.ui.Select):
    def __init__(self, *, owner: MatchResultSubmissionEditorView) -> None:
        self._owner = owner
        assigned = {row.entry_id for row in owner.draft.rows}
        options = [
            discord.SelectOption(
                label=assignment_choice_name(
                    entry_number=entry.entry_number,
                    game_account_name=entry.game_account_name,
                    horse_name=entry.horse_name,
                ),
                value=str(entry.entry_id),
            )
            for entry in _page_entries(owner.draft, page_index=owner.page_index)
            if entry.entry_id not in assigned
        ]
        super().__init__(
            custom_id="match-result-assign-entry",
            placeholder=assignment_placeholder(len(owner.draft.rows) + 1),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.assign(interaction, entry_id=int(self.values[0]))


class MatchResultCorrectionSelect(discord.ui.Select):
    def __init__(self, *, owner: MatchResultSubmissionEditorView) -> None:
        self._owner = owner
        rows_by_entry = {row.entry_id: row for row in owner.draft.rows}
        options = []
        for entry in _page_entries(owner.draft, page_index=owner.page_index):
            row = rows_by_entry.get(entry.entry_id)
            if row is not None:
                options.append(
                    discord.SelectOption(
                        label=correction_choice_name(
                            rank=row.rank,
                            entry_number=row.entry_number,
                            horse_name=row.horse_name,
                        ),
                        value=str(row.rank),
                    )
                )
        super().__init__(
            custom_id="match-result-correct-entry",
            placeholder=CORRECTION_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.open_correction(interaction, rank=int(self.values[0]))


class MatchResultCorrectionModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        owner: MatchResultSubmissionEditorView,
        rank: int,
    ) -> None:
        row = owner.draft.rows[rank - 1]
        super().__init__(title=correction_modal_title(rank), timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._owner = owner
        self._rank = rank
        self.entry_number = discord.ui.TextInput(
            label=ENTRY_NUMBER_LABEL,
            default=str(row.entry_number),
            required=True,
            max_length=10,
        )
        self.popularity_rank = discord.ui.TextInput(
            label=POPULARITY_LABEL,
            default=str(row.popularity_rank) if row.popularity_rank is not None else "",
            required=False,
            max_length=10,
        )
        if rank == 1:
            self.detail = discord.ui.TextInput(
                label=WINNER_TIME_LABEL,
                default=format_match_finish_time(owner.draft.finish_time_ms),
                required=False,
                max_length=16,
            )
        else:
            self.detail = discord.ui.TextInput(
                label=MARGIN_LABEL,
                default=row.margin or "",
                required=False,
                max_length=16,
            )
        self.add_item(self.entry_number)
        self.add_item(self.popularity_rank)
        self.add_item(self.detail)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._owner.correct(
            interaction,
            rank=self._rank,
            entry_number_value=str(self.entry_number.value),
            popularity_value=str(self.popularity_rank.value),
            detail=str(self.detail.value),
        )


class MatchResultPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        owner: MatchResultSubmissionEditorView,
        direction: int,
        disabled: bool,
    ) -> None:
        self._owner = owner
        self._direction = direction
        super().__init__(
            label=PREVIOUS_LABEL if direction < 0 else NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-result-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.change_page(interaction, direction=self._direction)


class MatchResultResetButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultSubmissionEditorView) -> None:
        self._owner = owner
        super().__init__(
            label=RESET_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-result-reset",
            disabled=not owner.draft.rows,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.reset(interaction)


class MatchResultSubmitButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultSubmissionEditorView) -> None:
        self._owner = owner
        all_seen = len(owner.seen_pages) == _page_count(owner.draft)
        super().__init__(
            label=SUBMIT_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="match-result-submit-final",
            disabled=not owner.draft.is_complete or not all_seen,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.submit(interaction)


class MatchResultCancelButton(discord.ui.Button):
    def __init__(self, *, owner: MatchResultSubmissionEditorView) -> None:
        self._owner = owner
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-result-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.cancel(interaction)


class MatchResultSubmissionEditorView(discord.ui.LayoutView):
    """Private adapter-local complete-board editor with no live UoW."""

    def __init__(
        self,
        *,
        adapter: MatchResultSubmissionDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchResultSubmissionDraft,
        page_index: int = 0,
        seen_pages: frozenset[int] | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.adapter = adapter
        self.context = context
        self.draft = draft
        pages = _page_count(draft)
        if not 0 <= page_index < pages:
            raise ValueError("page_index is outside the result draft.")
        self.page_index = page_index
        self.seen_pages = frozenset({page_index} if seen_pages is None else {*seen_pages, page_index})
        self._submission_started = False
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_match_result_submission_editor(
                    draft,
                    page_index=page_index,
                    seen_pages=self.seen_pages,
                )
            )
        )
        page_entries = _page_entries(draft, page_index=page_index)
        assigned = {row.entry_id for row in draft.rows}
        if not draft.is_complete and any(entry.entry_id not in assigned for entry in page_entries):
            assignment_row = discord.ui.ActionRow()
            assignment_row.add_item(MatchResultAssignmentSelect(owner=self))
            container.add_item(assignment_row)
        if draft.is_complete:
            correction_row = discord.ui.ActionRow()
            correction_row.add_item(MatchResultCorrectionSelect(owner=self))
            container.add_item(correction_row)
        navigation = discord.ui.ActionRow()
        navigation.add_item(MatchResultPageButton(owner=self, direction=-1, disabled=page_index == 0))
        navigation.add_item(
            MatchResultPageButton(
                owner=self,
                direction=1,
                disabled=page_index + 1 >= pages,
            )
        )
        navigation.add_item(MatchResultResetButton(owner=self))
        container.add_item(navigation)
        actions = discord.ui.ActionRow()
        actions.add_item(MatchResultSubmitButton(owner=self))
        actions.add_item(MatchResultCancelButton(owner=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_submission(self) -> bool:
        if self._submission_started:
            return False
        self._submission_started = True
        return True

    async def assign(self, interaction: discord.Interaction, *, entry_id: int) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        try:
            updated = self.draft.assign(entry_id=entry_id)
        except (TypeError, ValueError) as error:
            await self.adapter.send_component_error(interaction, str(error))
            return
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultSubmissionEditorView(
                adapter=self.adapter,
                context=self.context,
                draft=updated,
                page_index=self.page_index,
                seen_pages=self.seen_pages,
            ),
        )

    async def change_page(self, interaction: discord.Interaction, *, direction: int) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        next_page = self.page_index + direction
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultSubmissionEditorView(
                adapter=self.adapter,
                context=self.context,
                draft=self.draft,
                page_index=next_page,
                seen_pages=self.seen_pages,
            ),
        )

    async def open_correction(self, interaction: discord.Interaction, *, rank: int) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        try:
            await interaction.response.send_modal(MatchResultCorrectionModal(owner=self, rank=rank))
        except Exception:
            self.adapter.log_delivery_failure(interaction, "correction-modal")

    async def correct(
        self,
        interaction: discord.Interaction,
        *,
        rank: int,
        entry_number_value: str,
        popularity_value: str,
        detail: str,
    ) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        try:
            if not entry_number_value.isascii() or not entry_number_value.isdecimal():
                raise ValueError(ENTRY_NUMBER_ASCII_ERROR)
            entry_number = int(entry_number_value)
            normalized_popularity = popularity_value.strip()
            if normalized_popularity and (not normalized_popularity.isascii() or not normalized_popularity.isdecimal()):
                raise ValueError(POPULARITY_ASCII_ERROR)
            popularity = int(normalized_popularity) if normalized_popularity else None
            updated = self.draft.correct(
                rank=rank,
                entry_number=entry_number,
                popularity_rank=popularity,
                detail=detail.strip(),
            )
        except (TypeError, ValueError) as error:
            await self.adapter.send_component_error(interaction, str(error))
            return
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultSubmissionEditorView(
                adapter=self.adapter,
                context=self.context,
                draft=updated,
                page_index=self.page_index,
                seen_pages=frozenset({self.page_index}),
            ),
        )

    async def reset(self, interaction: discord.Interaction) -> None:
        if not await self.adapter.authorize_bound(interaction, context=self.context):
            return
        updated = MatchResultSubmissionDraft(target=self.draft.target, reason=self.draft.reason)
        self.stop()
        await self.adapter.edit_component(
            interaction,
            view=MatchResultSubmissionEditorView(
                adapter=self.adapter,
                context=self.context,
                draft=updated,
            ),
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.adapter.submit_draft(interaction, context=self.context, source_view=self)

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.adapter.cancel_draft(interaction, context=self.context, source_view=self)


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionDiscordAdapter:
    """Translate one bound staff result draft into query and command calls."""

    queries: MatchStaffResultSubmissionQueries
    commands: MatchResultSubmissionCommands
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        try:
            if not await self.authorize_autocomplete(interaction, _COMMAND_NAME):
                return []
            targets = await self.blocking_runner(lambda: self.queries.search_targets(search=current, limit=25))
            return match_result_submission_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def start_submission(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str | None,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(interaction, context=context, response_kind="submission-open"):
            return
        source_view.stop()
        try:
            target = await self.blocking_runner(lambda: self.queries.get_target(match_id=match_id))
            draft = MatchResultSubmissionDraft.from_target(target, reason=reason)
            if target.status == MatchStatus.RESULT_CONFIRMED and draft.reason is None:
                raise MatchResultSubmissionReasonRequiredError(CORRECTION_REASON_REQUIRED)
            await self._edit_layout(
                interaction,
                view=MatchResultSubmissionEditorView(
                    adapter=self,
                    context=context,
                    draft=draft,
                ),
            )
        except (MatchStaffResultSubmissionQueryError, MatchResultSubmissionError, TypeError, ValueError) as error:
            await self._edit_deferred(interaction, open_error(error))
        except Exception:
            self.log_application_failure(interaction)
            await self._edit_deferred(
                interaction,
                open_internal_error(correlation_id(interaction)),
            )

    async def submit_draft(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultSubmissionEditorView,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_SUBMIT_ERROR,
            )
            return
        if not await self.authorize_interaction(interaction, _COMMAND_NAME):
            return
        if len(source_view.seen_pages) != _page_count(source_view.draft):
            await self.send_component_error(interaction, REVIEW_REQUIRED)
            return
        if not await self._defer_message_update(interaction, response_kind="submission-final"):
            return
        if not source_view.claim_submission():
            source_view.stop()
            await self._edit_deferred(interaction, SUBMISSION_STARTED)
            return
        source_view.stop()
        try:
            interaction_id = _required_snowflake(
                getattr(interaction, "id", None),
                field_name="interaction ID",
            )
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.submit(
                    SaveMatchResultSubmission(
                        match_id=source_view.draft.target.match_id,
                        expected_state_fingerprint=source_view.draft.target.state_fingerprint,
                        candidate=source_view.draft.candidate(),
                        source_kind=MatchResultSourceKind.MANUAL,
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        idempotency_key=f"match-result-submit:{interaction_id}",
                        correlation_id=str(interaction_id),
                        reason=source_view.draft.reason,
                    )
                )
            )
        except MatchResultSubmissionIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchResultSubmissionStaleError:
            message = STALE
        except MatchResultSubmissionReasonRequiredError:
            message = REASON_REQUIRED
        except MatchResultSubmissionUnavailableError:
            message = UNAVAILABLE
        except (MatchResultSubmissionAuditError, MatchResultSubmissionInvalidSourceError):
            self.log_application_failure(interaction)
            message = evidence_error(correlation_id(interaction))
        except (MatchResultSubmissionError, TypeError, ValueError) as error:
            message = submission_error(error)
        except Exception:
            self.log_application_failure(interaction)
            message = submission_internal_error(correlation_id(interaction))
        else:
            message = format_match_result_submission_success(result)
        await self._edit_terminal(interaction, message=message)

    async def cancel_draft(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: MatchResultSubmissionEditorView,
    ) -> None:
        if not await self.authorize_bound(interaction, context=context):
            return
        source_view.stop()
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=self._terminal_layout(CANCELLED),
            )
        except Exception:
            self.log_delivery_failure(interaction, "cancel")

    async def authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(
                interaction,
                BOUND_INTERACTION_ERROR,
            )
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, response_kind=response_kind):
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _defer_message_update(
        self,
        interaction: discord.Interaction,
        *,
        response_kind: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            self.log_delivery_failure(interaction, f"{response_kind}-defer", error=error)
            return False
        return True

    @staticmethod
    async def edit_component(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
    ) -> None:
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def send_component_error(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component-error",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def _edit_layout(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=layout error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                type(error).__name__,
            )
            await MatchResultSubmissionDiscordAdapter._send_reopen_notice(interaction)

    @classmethod
    async def _edit_terminal(cls, interaction: discord.Interaction, *, message: str) -> None:
        await cls._edit_layout(interaction, view=cls._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, content: str) -> None:
        await cls._edit_terminal(interaction, message=content)

    @staticmethod
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)

    @staticmethod
    async def _send_reopen_notice(interaction: discord.Interaction) -> None:
        try:
            await interaction.followup.send(
                RESULT_TRANSITION_ERROR,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=reopen-notice",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    def log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def log_delivery_failure(
        interaction: object,
        response_kind: str,
        *,
        error: Exception | None = None,
    ) -> None:
        logger.error(
            "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            response_kind,
            type(error).__name__ if error is not None else "unknown",
        )
