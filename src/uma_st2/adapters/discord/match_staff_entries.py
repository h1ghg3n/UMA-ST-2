"""Discord adapter for complete native Match Entry-roster replacement."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchEntryAuditError,
    MatchEntryCandidateDraft,
    MatchEntryCharacterTarget,
    MatchEntryCommandError,
    MatchEntryCommands,
    MatchEntryIdempotencyConflictError,
    MatchEntryReplacementDraft,
    MatchEntryRosterSnapshot,
    MatchEntrySearchLine,
    MatchEntrySelection,
    MatchEntryStaleError,
    MatchEntryUnavailableError,
    MatchStaffEntryQueries,
    MatchStaffEntryQueryError,
    ReplaceMatchEntries,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
)
from .match_staff import MatchStaffInteractionContext
from .strings.match_staff_entries import (
    ACCOUNT_SEARCH_LABEL,
    BACK_LABEL,
    BOUND_CONFIRM_ERROR,
    BOUND_INTERACTION_ERROR,
    BOUND_SUBMIT_ERROR,
    CANCEL_LABEL,
    CANCELLED,
    CANDIDATE_ACCOUNT_PLACEHOLDER,
    CANDIDATE_CHARACTER_NEXT_LABEL,
    CANDIDATE_CHARACTER_PLACEHOLDER,
    CANDIDATE_CHARACTER_PREVIOUS_LABEL,
    CANDIDATE_CONFIRM_LABEL,
    CANDIDATE_SELECT_LABEL,
    CONFIRM_LABEL,
    CONFIRMATION_STARTED,
    IDEMPOTENCY_CONFLICT,
    INPUT_BUTTON_LABEL,
    INPUT_EMPTY_ERROR,
    INPUT_TYPE_ERROR,
    MODAL_LABEL,
    MODAL_PLACEHOLDER,
    MODAL_TITLE,
    NEXT_LABEL,
    NO_MATCHING_ACCOUNT,
    PREVIOUS_LABEL,
    REASON_TOO_LONG,
    REENTRY_LABEL,
    REOPEN_ENTRY,
    REVIEW_REQUIRED,
    SEARCH_CORRECTION_LABEL,
    SEARCH_CORRECTION_TITLE,
    STALE,
    UNAVAILABLE,
    entry_line_invalid_error,
    format_match_entry_candidate_detail,
    format_match_entry_candidate_roster,
    format_match_entry_draft,
    format_match_entry_input,
    format_match_entry_success,
    input_internal_error,
    input_notice,
    input_open_error,
    input_open_internal_error,
    match_entry_autocomplete_choices,
    preview_internal_error,
    reentry_notice,
    replacement_error,
    replacement_internal_error,
)

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.race-entries-set"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_PREVIEW_PAGE_SIZE = 8
_CHARACTER_PAGE_SIZE = 25


class ReturnToMatchRacePanel(Protocol):
    """Adapter-local callback to the existing race workflow navigation."""

    async def __call__(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def parse_match_entry_lines(value: str) -> tuple[MatchEntrySearchLine, ...]:
    """Parse one non-empty GameAccount nickname chunk per Entry line."""

    if not isinstance(value, str):
        raise ValueError(INPUT_TYPE_ERROR)
    parsed: list[MatchEntrySearchLine] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed.append(MatchEntrySearchLine(account_chunk=line))
        except ValueError as error:
            raise ValueError(entry_line_invalid_error(line_number, error)) from error
    if not parsed:
        raise ValueError(INPUT_EMPTY_ERROR)
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class MatchEntryCandidateSelectionDraft:
    """Adapter-local explicit selections over detached bounded candidates."""

    candidates: MatchEntryCandidateDraft
    account_ids: tuple[int | None, ...] = ()
    character_identities: tuple[tuple[int, int | None] | None, ...] = ()
    page: int = 0
    character_page: int = 0
    original_account_id: int | None = None
    original_character_identity: tuple[int, int | None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, MatchEntryCandidateDraft):
            raise ValueError("candidates must be MatchEntryCandidateDraft.")
        row_count = len(self.candidates.rows)
        account_ids = self.account_ids or (None,) * row_count
        character_identities = self.character_identities or (None,) * row_count
        if len(account_ids) != row_count or len(character_identities) != row_count:
            raise ValueError("candidate selections must match the candidate row count.")
        if not 0 <= self.page < row_count:
            raise ValueError("page is outside the candidate row range.")
        for row, account_id, identity in zip(
            self.candidates.rows,
            account_ids,
            character_identities,
            strict=True,
        ):
            if account_id is not None and account_id not in {item.id for item in row.accounts}:
                raise ValueError("selected GameAccount is outside its candidate row.")
            if identity is not None and identity not in {item.identity for item in self.candidates.characters}:
                raise ValueError("selected Umamusume is outside its candidate row.")
        if not 0 <= self.character_page < self.character_page_count:
            raise ValueError("character_page is outside the Umamusume page range.")
        object.__setattr__(self, "account_ids", tuple(account_ids))
        object.__setattr__(self, "character_identities", tuple(character_identities))

    @property
    def complete(self) -> bool:
        return all(item is not None for item in self.account_ids) and all(
            item is not None for item in self.character_identities
        )

    @property
    def search_lines(self) -> tuple[MatchEntrySearchLine, ...]:
        return tuple(row.search for row in self.candidates.rows)

    def with_account(self, account_id: int | None) -> MatchEntryCandidateSelectionDraft:
        updated = list(self.account_ids)
        updated[self.page] = account_id
        return replace(self, account_ids=tuple(updated))

    def with_character(
        self,
        identity: tuple[int, int | None] | None,
    ) -> MatchEntryCandidateSelectionDraft:
        updated = list(self.character_identities)
        updated[self.page] = identity
        return replace(self, character_identities=tuple(updated))

    def viewed(self, page: int) -> MatchEntryCandidateSelectionDraft:
        selected = self.character_identities[page]
        character_page = 0
        if selected is not None:
            character_page = next(
                index // _CHARACTER_PAGE_SIZE
                for index, item in enumerate(self.candidates.characters)
                if item.identity == selected
            )
        return replace(
            self,
            page=page,
            character_page=character_page,
            original_account_id=self.account_ids[page],
            original_character_identity=self.character_identities[page],
        )

    def with_character_page(self, page: int) -> MatchEntryCandidateSelectionDraft:
        return replace(self, character_page=page)

    @property
    def character_page_count(self) -> int:
        return max(1, (len(self.candidates.characters) + _CHARACTER_PAGE_SIZE - 1) // _CHARACTER_PAGE_SIZE)

    @property
    def visible_characters(self) -> tuple[MatchEntryCharacterTarget, ...]:
        start = self.character_page * _CHARACTER_PAGE_SIZE
        return self.candidates.characters[start : start + _CHARACTER_PAGE_SIZE]

    def selections(self) -> tuple[MatchEntrySelection, ...]:
        if not self.complete:
            raise ValueError("Every Entry candidate row must be selected.")
        return tuple(
            MatchEntrySelection(
                game_account_id=account_id,
                umamusume_id=identity[0],
                umamusume_variant_id=identity[1],
            )
            for account_id, identity in zip(self.account_ids, self.character_identities, strict=True)
            if account_id is not None and identity is not None
        )


@dataclass(frozen=True, slots=True)
class MatchEntryReplacementPreview:
    """PID-free closed query draft plus local pagination review state."""

    draft: MatchEntryReplacementDraft
    search_lines: tuple[MatchEntrySearchLine, ...]
    reason: str | None = None
    page: int = 0
    reviewed_pages: frozenset[int] = field(default_factory=lambda: frozenset({0}))

    def __post_init__(self) -> None:
        if not isinstance(self.draft, MatchEntryReplacementDraft):
            raise ValueError("draft must be MatchEntryReplacementDraft.")
        object.__setattr__(self, "search_lines", tuple(self.search_lines))
        if len(self.search_lines) != len(self.draft.desired_entries) or any(
            not isinstance(line, MatchEntrySearchLine) for line in self.search_lines
        ):
            raise ValueError("search_lines must match the desired Entry count.")
        reason = self.reason.strip() if isinstance(self.reason, str) else self.reason
        if reason == "":
            reason = None
        if reason is not None and (not isinstance(reason, str) or len(reason) > 255):
            raise ValueError(REASON_TOO_LONG)
        object.__setattr__(self, "reason", reason)
        if not 0 <= self.page < self.page_count:
            raise ValueError("page is outside the Entry preview range.")
        if not self.reviewed_pages or any(not 0 <= page < self.page_count for page in self.reviewed_pages):
            raise ValueError("reviewed_pages contains an invalid Entry preview page.")
        object.__setattr__(self, "reviewed_pages", frozenset((*self.reviewed_pages, self.page)))

    @property
    def page_count(self) -> int:
        row_count = max(len(self.draft.current.entries), len(self.draft.desired_entries))
        return max(1, (row_count + _PREVIEW_PAGE_SIZE - 1) // _PREVIEW_PAGE_SIZE)

    @property
    def all_pages_reviewed(self) -> bool:
        return self.reviewed_pages == frozenset(range(self.page_count))

    def viewed(self, page: int) -> MatchEntryReplacementPreview:
        return replace(self, page=page, reviewed_pages=frozenset((*self.reviewed_pages, page)))


def format_match_entry_preview(
    preview: MatchEntryReplacementPreview,
    *,
    notice: str | None = None,
) -> str:
    """Render one bounded PID-free before/after roster page."""

    return format_match_entry_draft(
        draft=preview.draft,
        page=preview.page,
        page_count=preview.page_count,
        all_pages_reviewed=preview.all_pages_reviewed,
        page_size=_PREVIEW_PAGE_SIZE,
        notice=notice,
    )


class MatchEntryInputButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        target: MatchEntryRosterSnapshot,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._target = target
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            label=INPUT_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id="match-entry-replacement-input",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_entry_modal(
            interaction,
            context=self._context,
            target=self._target,
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryInputView(discord.ui.LayoutView):
    """Bound zero-write launcher for the bulk Entry Modal."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        target: MatchEntryRosterSnapshot,
        reason: str | None,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_entry_input(target, notice=notice)))
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchEntryInputButton(
                adapter=adapter,
                context=context,
                target=target,
                reason=reason,
                source_view=self,
            )
        )
        actions.add_item(MatchEntryReturnButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)


class MatchEntryReplacementModal(discord.ui.Modal):
    """Bulk text form whose submit creates only a closed adapter draft."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        target: MatchEntryRosterSnapshot,
        reason: str | None,
        source_view: discord.ui.LayoutView,
        default_lines: tuple[MatchEntrySearchLine, ...] = (),
    ) -> None:
        super().__init__(title=MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._target = target
        self._reason = reason
        self._source_view = source_view
        self.entries = discord.ui.TextInput(
            label=MODAL_LABEL,
            placeholder=MODAL_PLACEHOLDER,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=4000,
            default="\n".join(line.account_chunk for line in default_lines) or None,
        )
        self.add_item(self.entries)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_candidates(
            interaction,
            context=self._context,
            target=self._target,
            raw_entries=str(self.entries.value),
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntrySearchCorrectionModal(discord.ui.Modal):
    """Correct only one unmatched search line in the detached roster draft."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        super().__init__(title=f"{draft.page + 1}번 {SEARCH_CORRECTION_TITLE}", timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        self.account_chunk = discord.ui.TextInput(
            label=SEARCH_CORRECTION_LABEL,
            min_length=1,
            max_length=100,
            default=draft.search_lines[draft.page].account_chunk,
        )
        self.add_item(self.account_chunk)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.correct_candidate_search(
            interaction,
            context=self._context,
            draft=self._draft,
            raw_chunk=str(self.account_chunk.value),
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryCandidateAccountSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        row = draft.candidates.rows[draft.page]
        super().__init__(
            placeholder=CANDIDATE_ACCOUNT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{item.game_region.value} · {item.nickname}"[:100],
                    value=str(item.id),
                    description=(f"소속: {item.affiliation or '없음'}")[:100],
                    default=item.id == draft.account_ids[draft.page],
                )
                for item in row.accounts
            ],
            custom_id="match-entry-candidate-account",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_candidate_account(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            account_id=int(self.values[0]),
            source_view=self._source_view,
        )


class MatchEntryCandidateCharacterSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            placeholder=CANDIDATE_CHARACTER_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=item.display_name[:100],
                    value=f"{item.umamusume_id}:{item.umamusume_variant_id or 0}",
                    default=item.identity == draft.character_identities[draft.page],
                )
                for item in draft.visible_characters
            ],
            custom_id="match-entry-candidate-character",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        base_id, variant_id = (int(value) for value in self.values[0].split(":", maxsplit=1))
        await self._adapter.update_candidate_character(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            identity=(base_id, variant_id or None),
            source_view=self._source_view,
        )


class MatchEntryCandidateEntryButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        entry_index: int,
        source_view: MatchEntryCandidateView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._entry_index = entry_index
        self._source_view = source_view
        row = draft.candidates.rows[entry_index]
        account = next((item for item in row.accounts if item.id == draft.account_ids[entry_index]), None)
        character = next(
            (item for item in draft.candidates.characters if item.identity == draft.character_identities[entry_index]),
            None,
        )
        status = " · ".join(
            (
                account.nickname if account is not None else "계정 미선택",
                character.display_name if character is not None else "우마무스메 미선택",
            )
        )
        super().__init__(
            label=(
                f"{row.entry_number}. {status}"
                if row.accounts
                else f"{row.entry_number}번 엔트리 : {NO_MATCHING_ACCOUNT}"
            )[:80],
            style=(
                discord.ButtonStyle.danger
                if not row.accounts
                else discord.ButtonStyle.success
                if account is not None and character is not None
                else discord.ButtonStyle.primary
            ),
            custom_id=f"match-entry-candidate-entry-{row.entry_number}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_candidate_entry(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            entry_index=self._entry_index,
            source_view=self._source_view,
        )


class MatchEntryCandidateCharacterPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        page: int,
        label: str,
        custom_id: str,
        source_view: MatchEntryCandidateDetailView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._page = page
        self._source_view = source_view
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_candidate_character_page(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            page=self._page,
            source_view=self._source_view,
        )


class MatchEntrySearchCorrectionButton(discord.ui.Button):
    """Reuse the single-line Modal when changing a saved Entry's account."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            label=ACCOUNT_SEARCH_LABEL, style=discord.ButtonStyle.secondary, custom_id="match-entry-search-correction"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_search_correction_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryCandidateBackButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateDetailView,
        cancel: bool = False,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        self._cancel = cancel
        super().__init__(
            label=CANCEL_LABEL if cancel else CANDIDATE_SELECT_LABEL,
            style=discord.ButtonStyle.secondary if cancel else discord.ButtonStyle.success,
            custom_id="match-entry-candidate-detail-cancel" if cancel else "match-entry-candidate-back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        draft = self._draft
        if self._cancel:
            draft = draft.with_account(draft.original_account_id).with_character(draft.original_character_identity)
        await self._adapter.show_candidate_roster(
            interaction,
            context=self._context,
            draft=draft,
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryCandidateReviewButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            label=CANDIDATE_CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="match-entry-candidate-review",
            disabled=not draft.complete,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.prepare_preview(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryCandidateReentryButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._reason = reason
        self._source_view = source_view
        super().__init__(
            label=REENTRY_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-entry-candidate-reentry",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_candidate_reentry_modal(
            interaction,
            context=self._context,
            draft=self._draft,
            reason=self._reason,
            source_view=self._source_view,
        )


class MatchEntryCandidateView(discord.ui.LayoutView):
    """Complete-roster summary with one edit button per Entry."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_match_entry_candidate_roster(
                    draft.candidates,
                    selected_account_ids=draft.account_ids,
                    selected_character_identities=draft.character_identities,
                    complete=draft.complete,
                    notice=notice,
                )
            )
        )
        for start in range(0, len(draft.candidates.rows), 5):
            entry_actions = discord.ui.ActionRow()
            for entry_index in range(start, min(start + 5, len(draft.candidates.rows))):
                entry_actions.add_item(
                    MatchEntryCandidateEntryButton(
                        adapter=adapter,
                        context=context,
                        draft=draft,
                        reason=reason,
                        entry_index=entry_index,
                        source_view=self,
                    )
                )
            container.add_item(entry_actions)
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchEntryCandidateReentryButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        actions.add_item(
            MatchEntryCandidateReviewButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        actions.add_item(MatchEntryReturnButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)


class MatchEntryCandidateDetailView(discord.ui.LayoutView):
    """One Entry's GameAccount and paged canonical Umamusume editor."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                format_match_entry_candidate_detail(
                    draft.candidates,
                    entry_index=draft.page,
                    selected_account_id=draft.account_ids[draft.page],
                    selected_character_identity=draft.character_identities[draft.page],
                    character_page=draft.character_page,
                    character_page_count=draft.character_page_count,
                )
            )
        )
        account_row = discord.ui.ActionRow()
        account_row.add_item(
            MatchEntryCandidateAccountSelect(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        character_row = discord.ui.ActionRow()
        character_row.add_item(
            MatchEntryCandidateCharacterSelect(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchEntryCandidateCharacterPageButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                page=max(0, draft.character_page - 1),
                label=CANDIDATE_CHARACTER_PREVIOUS_LABEL,
                custom_id="match-entry-character-page-이전",
                source_view=self,
                disabled=draft.character_page == 0,
            )
        )
        actions.add_item(
            MatchEntryCandidateCharacterPageButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                page=min(draft.character_page_count - 1, draft.character_page + 1),
                label=CANDIDATE_CHARACTER_NEXT_LABEL,
                custom_id="match-entry-character-page-다음",
                source_view=self,
                disabled=draft.character_page == draft.character_page_count - 1,
            )
        )
        actions.add_item(
            MatchEntrySearchCorrectionButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        actions.add_item(
            MatchEntryCandidateBackButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
            )
        )
        actions.add_item(
            MatchEntryCandidateBackButton(
                adapter=adapter,
                context=context,
                draft=draft,
                reason=reason,
                source_view=self,
                cancel=True,
            )
        )
        container.add_item(account_row)
        container.add_item(character_row)
        container.add_item(actions)
        self.add_item(container)


class MatchEntryPageButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        page: int,
        label: str,
        source_view: MatchEntryPreviewView,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._page = page
        self._source_view = source_view
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"match-entry-replacement-page-{label}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview_page(
            interaction,
            context=self._context,
            preview=self._preview.viewed(self._page),
            source_view=self._source_view,
        )


class MatchEntryReentryButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        source_view: MatchEntryPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=REENTRY_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="match-entry-replacement-reentry",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_reentry_modal(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchEntryConfirmButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        source_view: MatchEntryPreviewView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        self._source_view = source_view
        super().__init__(
            label=CONFIRM_LABEL,
            style=discord.ButtonStyle.success,
            custom_id="match-entry-replacement-confirm",
            disabled=not preview.all_pages_reviewed,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.confirm_replacement(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self._source_view,
        )


class MatchEntryReturnButton(discord.ui.Button):
    """Discard the local editor and return to a freshly composed race panel."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(label=BACK_LABEL, style=discord.ButtonStyle.secondary, custom_id="match-entry-race-back")

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.return_to_race_panel(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchEntryCancelButton(discord.ui.Button):
    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._source_view = source_view
        super().__init__(
            label=CANCEL_LABEL,
            style=discord.ButtonStyle.danger,
            custom_id="match-entry-replacement-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel_replacement(
            interaction,
            context=self._context,
            source_view=self._source_view,
        )


class MatchEntryPreviewView(discord.ui.LayoutView):
    """PID-free paged preview that retains no Session or transaction."""

    def __init__(
        self,
        *,
        adapter: MatchEntryDiscordAdapter,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.preview = preview
        self._confirmation_started = False
        container = discord.ui.Container(discord.ui.TextDisplay(format_match_entry_preview(preview, notice=notice)))
        if preview.page_count > 1:
            pages = discord.ui.ActionRow()
            pages.add_item(
                MatchEntryPageButton(
                    adapter=adapter,
                    context=context,
                    preview=preview,
                    page=max(0, preview.page - 1),
                    label=PREVIOUS_LABEL,
                    source_view=self,
                    disabled=preview.page == 0,
                )
            )
            pages.add_item(
                discord.ui.Button(
                    label=f"{preview.page + 1} / {preview.page_count}",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                )
            )
            pages.add_item(
                MatchEntryPageButton(
                    adapter=adapter,
                    context=context,
                    preview=preview,
                    page=min(preview.page_count - 1, preview.page + 1),
                    label=NEXT_LABEL,
                    source_view=self,
                    disabled=preview.page == preview.page_count - 1,
                )
            )
            container.add_item(pages)
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchEntryReentryButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(
            MatchEntryConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
                source_view=self,
            )
        )
        actions.add_item(MatchEntryCancelButton(adapter=adapter, context=context, source_view=self))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


@dataclass(frozen=True, slots=True)
class MatchEntryDiscordAdapter:
    """Translate a bound staff flow into query and command runner calls."""

    queries: MatchStaffEntryQueries
    commands: MatchEntryCommands
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    return_to_race_panel: ReturnToMatchRacePanel
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
            return match_entry_autocomplete_choices(targets)
        except Exception:
            logger.error(
                "Discord autocomplete failed correlation_id=%s command=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
            )
            return []

    async def start_replacement(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        match_id: int,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-replacement-open",
        ):
            return
        try:
            prepared = await self.blocking_runner(lambda: self.queries.prepare_editor(match_id=match_id))
            normalized_reason = reason.strip() if isinstance(reason, str) else reason
            if normalized_reason == "":
                normalized_reason = None
            if normalized_reason is not None and len(normalized_reason) > 255:
                raise ValueError(REASON_TOO_LONG)
            if isinstance(prepared, MatchEntryCandidateDraft):
                draft = MatchEntryCandidateSelectionDraft(
                    candidates=prepared,
                    account_ids=tuple(entry.game_account_id for entry in prepared.current.entries),
                    character_identities=tuple(
                        (entry.umamusume_id, entry.umamusume_variant_id) for entry in prepared.current.entries
                    ),
                )
                view: discord.ui.LayoutView = MatchEntryCandidateView(
                    adapter=self,
                    context=context,
                    draft=draft,
                    reason=normalized_reason,
                )
            else:
                view = MatchEntryInputView(
                    adapter=self,
                    context=context,
                    target=prepared,
                    reason=normalized_reason,
                )
            source_view.stop()
            await self._edit_layout(
                interaction,
                view=view,
                response_kind="entry-replacement-open",
            )
        except (MatchStaffEntryQueryError, TypeError, ValueError) as error:
            await self._send_component_error(interaction, input_open_error(error))
        except Exception:
            self._log_application_failure(interaction)
            await self._send_component_error(
                interaction,
                input_open_internal_error(correlation_id(interaction)),
            )

    async def open_entry_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        target: MatchEntryRosterSnapshot,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                MatchEntryReplacementModal(
                    adapter=self,
                    context=context,
                    target=target,
                    reason=reason,
                    source_view=source_view,
                )
            )
        except Exception as error:
            self._log_delivery_failure(interaction, "entry-modal", error=error)

    async def show_candidates(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        target: MatchEntryRosterSnapshot,
        raw_entries: str,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-candidate-prepare",
            mismatch_message=BOUND_SUBMIT_ERROR,
        ):
            return
        try:
            lines = parse_match_entry_lines(raw_entries)
            candidates = await self.blocking_runner(
                lambda: self.queries.prepare_candidates(match_id=target.match_id, lines=lines)
            )
            draft = MatchEntryCandidateSelectionDraft(candidates=candidates)
            source_view.stop()
            await self._edit_layout(
                interaction,
                view=MatchEntryCandidateView(
                    adapter=self,
                    context=context,
                    draft=draft,
                    reason=reason,
                ),
            )
        except (MatchStaffEntryQueryError, TypeError, ValueError) as error:
            view = MatchEntryInputView(
                adapter=self,
                context=context,
                target=target,
                reason=reason,
                notice=input_notice(error),
            )
            source_view.stop()
            await self._edit_layout(interaction, view=view)
        except Exception:
            self._log_application_failure(interaction)
            await self._send_component_error(
                interaction,
                input_internal_error(correlation_id(interaction)),
            )

    async def open_reentry_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        source_view: MatchEntryPreviewView,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                MatchEntryReplacementModal(
                    adapter=self,
                    context=context,
                    target=preview.draft.current,
                    reason=preview.reason,
                    source_view=source_view,
                    default_lines=preview.search_lines,
                )
            )
        except Exception as error:
            self._log_delivery_failure(interaction, "entry-reentry-modal", error=error)

    async def open_candidate_reentry_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateView,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                MatchEntryReplacementModal(
                    adapter=self,
                    context=context,
                    target=draft.candidates.current,
                    reason=reason,
                    source_view=source_view,
                    default_lines=draft.search_lines,
                )
            )
        except Exception as error:
            self._log_delivery_failure(interaction, "entry-candidate-reentry-modal", error=error)

    async def update_candidate_account(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        account_id: int,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        try:
            updated = draft.with_account(account_id)
            view = MatchEntryCandidateDetailView(
                adapter=self,
                context=context,
                draft=updated,
                reason=reason,
            )
        except (TypeError, ValueError):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-candidate-account",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-candidate-account",
        )

    async def update_candidate_character(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        identity: tuple[int, int | None],
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        try:
            updated = draft.with_character(identity)
            view = MatchEntryCandidateDetailView(
                adapter=self,
                context=context,
                draft=updated,
                reason=reason,
            )
        except (TypeError, ValueError):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-candidate-character",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-candidate-character",
        )

    async def show_candidate_entry(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        entry_index: int,
        source_view: MatchEntryCandidateView,
    ) -> None:
        try:
            updated = draft.viewed(entry_index)
            if not updated.candidates.rows[entry_index].accounts:
                await self.open_search_correction_modal(
                    interaction,
                    context=context,
                    draft=updated,
                    reason=reason,
                    source_view=source_view,
                )
                return
            view = MatchEntryCandidateDetailView(
                adapter=self,
                context=context,
                draft=updated,
                reason=reason,
            )
        except (TypeError, ValueError):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-candidate-open",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-candidate-open",
        )

    async def open_search_correction_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        if source_view.is_finished():
            await self._send_component_error(interaction, REOPEN_ENTRY)
            return
        try:
            await interaction.response.send_modal(
                MatchEntrySearchCorrectionModal(
                    adapter=self, context=context, draft=draft, reason=reason, source_view=source_view
                )
            )
        except Exception as error:
            self._log_delivery_failure(interaction, "entry-search-correction-modal", error=error)

    async def correct_candidate_search(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        raw_chunk: str,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-search-correction",
        ):
            return
        if source_view.is_finished():
            await self._send_component_error(interaction, REOPEN_ENTRY)
            return
        try:
            lines = list(draft.search_lines)
            lines[draft.page] = MatchEntrySearchLine(raw_chunk)
            candidates = await self.blocking_runner(
                lambda: self.queries.prepare_candidates(match_id=draft.candidates.current.match_id, lines=tuple(lines))
            )
            updated = MatchEntryCandidateSelectionDraft(
                candidates=candidates,
                account_ids=tuple(
                    selected if index != draft.page and selected in {item.id for item in row.accounts} else None
                    for index, (row, selected) in enumerate(zip(candidates.rows, draft.account_ids, strict=True))
                ),
                character_identities=tuple(
                    selected if selected in {item.identity for item in candidates.characters} else None
                    for index, selected in enumerate(draft.character_identities)
                ),
            )
            view = MatchEntryCandidateView(adapter=self, context=context, draft=updated, reason=reason)
        except (MatchStaffEntryQueryError, TypeError, ValueError) as error:
            await self._send_component_error(interaction, input_notice(error))
            return
        except Exception:
            self._log_application_failure(interaction)
            await self._send_component_error(interaction, input_internal_error(correlation_id(interaction)))
            return
        if source_view.is_finished():
            view.stop()
            await self._send_component_error(interaction, REOPEN_ENTRY)
            return
        source_view.stop()
        await self._edit_layout(interaction, view=view, response_kind="entry-search-correction")

    async def show_candidate_character_page(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        page: int,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        try:
            updated = draft.with_character_page(page)
            view = MatchEntryCandidateDetailView(
                adapter=self,
                context=context,
                draft=updated,
                reason=reason,
            )
        except (TypeError, ValueError):
            await self._send_component_error(interaction, BOUND_INTERACTION_ERROR)
            return
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-character-page",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-character-page",
        )

    async def show_candidate_roster(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateDetailView,
    ) -> None:
        view = MatchEntryCandidateView(
            adapter=self,
            context=context,
            draft=draft,
            reason=reason,
        )
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-candidate-roster",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-candidate-roster",
        )

    async def prepare_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchEntryCandidateSelectionDraft,
        reason: str | None,
        source_view: MatchEntryCandidateView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-preview-prepare",
            mismatch_message=BOUND_CONFIRM_ERROR,
        ):
            return
        try:
            replacement = await self.blocking_runner(
                lambda: self.queries.prepare_replacement(
                    match_id=draft.candidates.current.match_id,
                    selections=draft.selections(),
                )
            )
            preview = MatchEntryReplacementPreview(
                draft=replacement,
                search_lines=draft.search_lines,
                reason=reason,
            )
            view: discord.ui.LayoutView = MatchEntryPreviewView(
                adapter=self,
                context=context,
                preview=preview,
            )
        except (MatchStaffEntryQueryError, TypeError, ValueError) as error:
            view = MatchEntryCandidateView(
                adapter=self,
                context=context,
                draft=draft,
                reason=reason,
                notice=reentry_notice(error),
            )
        except Exception:
            self._log_application_failure(interaction)
            await self._send_component_error(
                interaction,
                preview_internal_error(correlation_id(interaction)),
            )
            return
        source_view.stop()
        await self._edit_layout(interaction, view=view)

    async def show_preview_page(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        source_view: MatchEntryPreviewView,
    ) -> None:
        view = MatchEntryPreviewView(adapter=self, context=context, preview=preview)
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-preview-page",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-preview-page",
        )

    async def confirm_replacement(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        preview: MatchEntryReplacementPreview,
        source_view: MatchEntryPreviewView,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-replacement-confirm",
            mismatch_message=BOUND_CONFIRM_ERROR,
        ):
            return
        if not preview.all_pages_reviewed:
            source_view.stop()
            await self._edit_layout(
                interaction,
                view=MatchEntryPreviewView(
                    adapter=self,
                    context=context,
                    preview=preview,
                    notice=REVIEW_REQUIRED,
                ),
            )
            return
        if not source_view.claim_confirmation():
            await self._edit_deferred(interaction, CONFIRMATION_STARTED)
            return
        try:
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            result = await self.blocking_runner(
                lambda: self.commands.replace_entries(
                    ReplaceMatchEntries(
                        match_id=preview.draft.current.match_id,
                        entries=preview.draft.command_entries,
                        expected_roster_fingerprint=preview.draft.current.roster_fingerprint,
                        idempotency_key=f"match-entry-replacement:{interaction_id}",
                        actor_discord_user_id=str(actor_id),
                        guild_id=str(context.guild_id),
                        correlation_id=str(interaction_id),
                        reason=preview.reason,
                    )
                )
            )
        except MatchEntryIdempotencyConflictError:
            message = IDEMPOTENCY_CONFLICT
        except MatchEntryStaleError:
            message = STALE
        except MatchEntryUnavailableError:
            message = UNAVAILABLE
        except (MatchEntryAuditError, MatchEntryCommandError, TypeError, ValueError) as error:
            message = replacement_error(error)
        except Exception:
            self._log_application_failure(interaction)
            message = replacement_internal_error(correlation_id(interaction))
        else:
            message = format_match_entry_success(result)
        source_view.stop()
        await self._edit_terminal(interaction, message=message)

    async def cancel_replacement(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None:
        view = self._terminal_layout(CANCELLED)
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="entry-replacement-cancel",
        ):
            return
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="entry-replacement-cancel",
        )

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(
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
        mismatch_message: str = BOUND_INTERACTION_ERROR,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, mismatch_message)
            return False
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            self._log_delivery_failure(
                interaction,
                f"{response_kind}-defer",
                error=error,
            )
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _edit_layout(
        self,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        response_kind: str = "layout",
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
                "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            await self._send_component_error(interaction, REOPEN_ENTRY)

    async def _edit_terminal(self, interaction: discord.Interaction, *, message: str) -> None:
        await self._edit_layout(interaction, view=self._terminal_layout(message))

    @classmethod
    async def _edit_deferred(cls, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=cls._terminal_layout(content),
            )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=deferred",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
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
    def _terminal_layout(message: str) -> discord.ui.LayoutView:
        return buttonless_terminal_layout(message, timeout_seconds=_COMPONENT_TIMEOUT_SECONDS)

    @staticmethod
    def _log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )

    @staticmethod
    def _log_delivery_failure(
        interaction: object,
        response_kind: str,
        *,
        error: Exception,
    ) -> None:
        logger.error(
            "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
            response_kind,
            type(error).__name__,
        )
