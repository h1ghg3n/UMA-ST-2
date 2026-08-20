from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.autocomplete_queries import AutocompleteChoice, RoomRaceChoicePurpose
from umacircle_bot.services.dtos import RaceOperationResultDTO
from umacircle_bot.services.match_entry_resolution import (
    MatchEntryCandidateSetDTO,
    MatchEntryDraftInput,
    MatchEntryGameAccountCandidateDTO,
    MatchRaceResolvedEntriesCommand,
    ResolvedMatchEntryInput,
)

ENTRIES_COMMAND_NAME = "match.staff.entries-set"
ENTRIES_PER_PAGE = 9
DRAFT_TIMEOUT_SECONDS = 600


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class PrepareComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendCommandErrorPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> None: ...


class SendInitialResponsePort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str, content: str) -> bool: ...


class SendRaceResultPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        result: RaceOperationResultDTO,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MatchStaffEntryAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_race_choices: Callable[[RoomRaceChoicePurpose], tuple[AutocompleteChoice, ...]]
    parse_draft: Callable[[str], tuple[MatchEntryDraftInput, ...]]
    query_candidates: Callable[[tuple[str, ...]], tuple[MatchEntryCandidateSetDTO, ...]]
    execute_replacement: Callable[[MatchRaceResolvedEntriesCommand], RaceOperationResultDTO]
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    handle_modal_open_error: SendCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_race_result: SendRaceResultPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    logger: logging.Logger


@dataclass(slots=True)
class _DraftEntry:
    query: str
    character_name: str
    candidates: tuple[MatchEntryGameAccountCandidateDTO, ...]
    selected_game_account_id: int | None = None

    def selected_candidate(self) -> MatchEntryGameAccountCandidateDTO | None:
        if self.selected_game_account_id is None:
            return None
        return next(
            (candidate for candidate in self.candidates if candidate.game_account_id == self.selected_game_account_id),
            None,
        )


class MatchStaffEntryAdapter:
    """Persistence-free operator resolution flow for native Match Entry snapshots."""

    def __init__(self, ports: MatchStaffEntryAdapterPorts) -> None:
        self.ports = ports

    async def entries_set(self, interaction: discord.Interaction) -> None:
        if await self.ports.prepare_command(interaction, ENTRIES_COMMAND_NAME, defer=False) is None:
            return
        try:
            choices = await run_blocking_application(
                lambda: self.ports.query_race_choices(RoomRaceChoicePurpose.ENTRIES)
            )
            if not choices:
                await self.ports.send_initial_response(
                    interaction,
                    ENTRIES_COMMAND_NAME,
                    "현재 엔트리를 교체할 수 있는 룸매치 경기가 없습니다.",
                )
                return
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(
                MatchEntryDraftModal(
                    adapter=self,
                    choices=choices,
                    context=context,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "경기 목록을 불러오지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, ENTRIES_COMMAND_NAME)

    async def begin_resolution(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        entries_text: str,
        reason: str,
    ) -> None:
        if not await self._prepare_component(interaction, context):
            return
        try:
            race_id = _selected_id(selected_values, allowed_ids)
            parsed = self.ports.parse_draft(entries_text)
            candidate_sets = await run_blocking_application(
                lambda: self.ports.query_candidates(tuple(entry.game_account_query for entry in parsed))
            )
            entries = _build_draft_entries(parsed, candidate_sets)
            operation_id = self.ports.correlation_id(interaction)
            view = MatchEntryResolutionView(
                adapter=self,
                context=context,
                race_id=race_id,
                entries=entries,
                reason=_optional_text(reason),
                operation_id=operation_id,
            )
            await interaction.response.send_message(ephemeral=True, view=view)
            try:
                view.bind_message(await interaction.original_response())
            except Exception:
                log_sanitized_exception(
                    self.ports.logger,
                    "match entry draft response lookup failed correlation_id=%s",
                    operation_id,
                )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "엔트리 후보를 준비하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)

    async def revise_resolution(
        self,
        interaction: discord.Interaction,
        *,
        view: MatchEntryResolutionView,
        entries_text: str,
        reason: str,
    ) -> None:
        if not await self._prepare_component(interaction, view.context):
            return
        if not view.is_active:
            await _respond_stale(interaction)
            return
        try:
            parsed = self.ports.parse_draft(entries_text)
            candidate_sets = await run_blocking_application(
                lambda: self.ports.query_candidates(tuple(entry.game_account_query for entry in parsed))
            )
            view.replace_draft(
                entries=_build_draft_entries(parsed, candidate_sets),
                reason=_optional_text(reason),
            )
            await interaction.response.edit_message(view=view)
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "엔트리 입력을 수정하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)

    async def resolve_entry(
        self,
        interaction: discord.Interaction,
        *,
        view: MatchEntryResolutionView,
        entry_index: int,
        game_account_id: int,
    ) -> None:
        if not await self._prepare_component(interaction, view.context):
            return
        if not view.is_active:
            await _respond_stale(interaction)
            return
        try:
            view.select(entry_index, game_account_id)
            await interaction.response.edit_message(view=view)
        except ValueError as exc:
            await self.ports.send_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "GameAccount 선택을 반영하지 못했습니다",
                exc,
            )
        except Exception:
            await self.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)

    async def commit(
        self,
        interaction: discord.Interaction,
        *,
        view: MatchEntryResolutionView,
    ) -> None:
        if not view.begin_commit():
            await _respond_stale(interaction)
            return
        await interaction.response.edit_message(view=view)
        try:
            command = MatchRaceResolvedEntriesCommand(
                race_id=view.race_id,
                entries=view.resolved_entries(),
                reason=view.reason,
                actor_discord_user_id=str(interaction.user.id),
                interaction_id=view.operation_id,
            )
            result = await run_blocking_application(lambda: self.ports.execute_replacement(command))
        except (DomainError, ValueError) as exc:
            view.restore_after_failed_commit()
            await _edit_original_view(interaction, view)
            await self.ports.send_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "확정 엔트리를 설정하지 못했습니다",
                exc,
            )
            return
        except Exception:
            view.restore_after_failed_commit()
            await _edit_original_view(interaction, view)
            await self.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)
            return

        view.finish("committed")
        await self.ports.send_race_result(interaction, ENTRIES_COMMAND_NAME, result)
        await _delete_original_view(interaction)

    async def authorize_view(
        self,
        interaction: discord.Interaction,
        *,
        view: MatchEntryResolutionView,
    ) -> bool:
        if not view.is_active:
            await _respond_stale(interaction)
            return False
        return await self._prepare_component(interaction, view.context)

    async def _prepare_component(self, interaction: discord.Interaction, context: InteractionContext) -> bool:
        return (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=ENTRIES_COMMAND_NAME,
                defer=False,
            )
            is not None
        )


class MatchEntryDraftModal(discord.ui.Modal, title="룸매치 엔트리 입력"):
    def __init__(
        self,
        *,
        adapter: MatchStaffEntryAdapter,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=DRAFT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = _race_select(
            custom_id="match-entry-resolution-target",
            placeholder="엔트리를 교체할 경기 선택",
            choices=choices,
        )
        self.entries = discord.ui.TextInput(
            custom_id="match-entry-resolution-input",
            placeholder="zener | 스페셜 위크\nalice | 라이스 샤워",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="match-entry-resolution-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 엔트리 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(
            discord.ui.Label(
                text="GameAccount 이름 일부 | 우마무스메명",
                description="검색어는 후보 정렬에만 사용되며 실제 계정은 다음 화면에서 직접 선택합니다.",
                component=self.entries,
            )
        )
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.begin_resolution(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            entries_text=str(self.entries.value),
            reason=str(self.reason.value),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self._adapter.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)


class MatchEntryResolutionView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: MatchStaffEntryAdapter,
        context: InteractionContext,
        race_id: int,
        entries: list[_DraftEntry],
        reason: str | None,
        operation_id: str,
    ) -> None:
        super().__init__(timeout=DRAFT_TIMEOUT_SECONDS)
        self.adapter = adapter
        self.context = context
        self.race_id = race_id
        self.entries = entries
        self.reason = reason
        self.operation_id = operation_id
        self.page = 0
        self.state = "active"
        self._message: object | None = None
        self._rebuild()

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + ENTRIES_PER_PAGE - 1) // ENTRIES_PER_PAGE)

    def bind_message(self, message: object) -> None:
        self._message = message

    def select(self, entry_index: int, game_account_id: int) -> None:
        if not self.is_active:
            raise ValueError("이미 종료된 엔트리 draft입니다.")
        if not 0 <= entry_index < len(self.entries):
            raise ValueError("엔트리 선택 대상이 올바르지 않습니다.")
        entry = self.entries[entry_index]
        if game_account_id not in {candidate.game_account_id for candidate in entry.candidates}:
            raise ValueError("현재 후보 목록에 없는 GameAccount입니다.")
        entry.selected_game_account_id = game_account_id
        self._rebuild()

    def replace_draft(self, *, entries: list[_DraftEntry], reason: str | None) -> None:
        if not self.is_active:
            raise ValueError("이미 종료된 엔트리 draft입니다.")
        self.entries = entries
        self.reason = reason
        self.page = 0
        self._rebuild()

    def input_text(self) -> str:
        return "\n".join(f"{entry.query} | {entry.character_name}" for entry in self.entries)

    def begin_commit(self) -> bool:
        if not self.is_active or not self._can_commit():
            return False
        self.state = "committing"
        self._rebuild()
        return True

    def restore_after_failed_commit(self) -> None:
        if self.state == "committing":
            self.state = "active"
            self._rebuild()

    def finish(self, state: str) -> None:
        if state not in {"committed", "cancelled", "timed_out"}:
            raise ValueError("invalid terminal draft state")
        self.state = state
        self._rebuild()
        self.stop()

    def resolved_entries(self) -> tuple[ResolvedMatchEntryInput, ...]:
        if self.state != "committing":
            raise ValueError("엔트리 draft가 확정 처리 중이 아닙니다.")
        selected = [entry.selected_game_account_id for entry in self.entries]
        if any(value is None for value in selected):
            raise ValueError("모든 엔트리의 GameAccount를 선택해야 합니다.")
        ids = tuple(int(value) for value in selected if value is not None)
        if len(ids) != len(set(ids)):
            raise ValueError("같은 GameAccount를 한 Race에 두 번 등록할 수 없습니다.")
        return tuple(
            ResolvedMatchEntryInput(
                game_account_id=game_account_id,
                character_name=entry.character_name,
            )
            for entry, game_account_id in zip(self.entries, ids, strict=True)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.adapter.authorize_view(interaction, view=self)

    async def on_timeout(self) -> None:
        if not self.is_active:
            return
        self.finish("timed_out")
        edit = getattr(self._message, "edit", None)
        if callable(edit):
            try:
                await edit(view=self)
            except Exception:
                log_sanitized_exception(
                    self.adapter.ports.logger,
                    "match entry draft timeout edit failed operation_id=%s",
                    self.operation_id,
                )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log_sanitized_exception(
            self.adapter.ports.logger,
            "match entry resolution view failed operation_id=%s actor_id=%s",
            self.operation_id,
            self.adapter.ports.interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await self.adapter.ports.send_internal_error(interaction, ENTRIES_COMMAND_NAME)

    def _can_commit(self) -> bool:
        ids = [entry.selected_game_account_id for entry in self.entries]
        return bool(ids) and all(value is not None for value in ids) and len(ids) == len(set(ids))

    def _rebuild(self) -> None:
        self.clear_items()
        page_start = self.page * ENTRIES_PER_PAGE
        page_entries = self.entries[page_start : page_start + ENTRIES_PER_PAGE]
        duplicate_ids = _duplicate_selected_ids(self.entries)
        body: list[discord.ui.Item | str] = [
            discord.ui.TextDisplay(
                f"### 룸매치 엔트리 GameAccount 확인\n"
                f"Race #{self.race_id} · {len(self.entries)}명 · {self.page + 1}/{self.page_count} 페이지\n"
                "검색 순서는 참고용입니다. 각 엔트리의 실제 GameAccount를 운영자가 직접 선택해야 합니다."
            )
        ]
        for offset, entry in enumerate(page_entries):
            index = page_start + offset
            selected = entry.selected_candidate()
            if selected is None:
                status = "미선택" if entry.candidates else "후보 없음 · 입력 수정 필요"
            else:
                status = _candidate_label(selected)
                if selected.game_account_id in duplicate_ids:
                    status += " · ⚠️ 중복 선택"
            button = _ResolveButton(
                entry_index=index,
                label="변경" if selected is not None else "선택",
                disabled=self.state != "active" or not entry.candidates,
            )
            body.append(
                discord.ui.Section(
                    f"**{index + 1}. {entry.query} | {entry.character_name}**\n{status}",
                    accessory=button,
                )
            )
        self.add_item(discord.ui.Container(*body))
        self.add_item(
            discord.ui.ActionRow(
                _PreviousButton(disabled=self.state != "active" or self.page == 0),
                _NextButton(disabled=self.state != "active" or self.page >= self.page_count - 1),
                _EditDraftButton(disabled=self.state != "active"),
                _CancelDraftButton(disabled=self.state != "active"),
                _CommitDraftButton(disabled=self.state != "active" or not self._can_commit()),
            )
        )


class _ResolveButton(discord.ui.Button):
    def __init__(self, *, entry_index: int, label: str, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, disabled=disabled)
        self.entry_index = entry_index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = _resolution_view(self)
        entry = view.entries[self.entry_index]
        if not entry.candidates:
            await interaction.response.send_message("검색 후보가 없습니다. 입력을 수정해 주세요.", ephemeral=True)
            return
        await interaction.response.send_modal(
            MatchEntryCandidateModal(
                view=view,
                entry_index=self.entry_index,
            )
        )


class _PreviousButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="이전", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = _resolution_view(self)
        view.page = max(0, view.page - 1)
        view._rebuild()
        await interaction.response.edit_message(view=view)


class _NextButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="다음", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = _resolution_view(self)
        view.page = min(view.page_count - 1, view.page + 1)
        view._rebuild()
        await interaction.response.edit_message(view=view)


class _EditDraftButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="입력 수정", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MatchEntryDraftEditModal(view=_resolution_view(self)))


class _CancelDraftButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="취소", style=discord.ButtonStyle.danger, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = _resolution_view(self)
        view.finish("cancelled")
        await interaction.response.defer(ephemeral=True)
        await _delete_original_view(interaction)


class _CommitDraftButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="전체 확정", style=discord.ButtonStyle.success, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _resolution_view(self).adapter.commit(interaction, view=_resolution_view(self))


class MatchEntryCandidateModal(discord.ui.Modal, title="GameAccount 선택"):
    def __init__(self, *, view: MatchEntryResolutionView, entry_index: int) -> None:
        super().__init__(timeout=DRAFT_TIMEOUT_SECONDS)
        self._resolution_view = view
        self._entry_index = entry_index
        entry = view.entries[entry_index]
        self.account = discord.ui.Select(
            custom_id=f"match-entry-candidate-{entry_index}",
            placeholder="실제 GameAccount 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_candidate_option_label(candidate),
                    value=str(candidate.game_account_id),
                    default=candidate.game_account_id == entry.selected_game_account_id,
                )
                for candidate in entry.candidates
            ],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text=f"{entry_index + 1}. {entry.query} | {entry.character_name}",
                description="유사도 정렬은 identity 확정이 아닙니다. 실제 계정을 직접 선택해 주세요.",
                component=self.account,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = tuple(self.account.values)
        if len(values) != 1 or not values[0].isdigit():
            await self._resolution_view.adapter.ports.send_user_error(
                interaction,
                ENTRIES_COMMAND_NAME,
                "GameAccount 선택을 반영하지 못했습니다",
                ValueError("GameAccount를 하나 선택해야 합니다."),
            )
            return
        await self._resolution_view.adapter.resolve_entry(
            interaction,
            view=self._resolution_view,
            entry_index=self._entry_index,
            game_account_id=int(values[0]),
        )


class MatchEntryDraftEditModal(discord.ui.Modal, title="룸매치 엔트리 입력 수정"):
    def __init__(self, *, view: MatchEntryResolutionView) -> None:
        super().__init__(timeout=DRAFT_TIMEOUT_SECONDS)
        self._resolution_view = view
        self.entries = discord.ui.TextInput(
            custom_id="match-entry-resolution-edit-input",
            label="GameAccount 이름 일부 | 우마무스메명",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
            default=view.input_text(),
        )
        self.reason = discord.ui.TextInput(
            custom_id="match-entry-resolution-edit-reason",
            label="운영 메모 (선택)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
            default=view.reason or "",
        )
        self.add_item(self.entries)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._resolution_view.adapter.revise_resolution(
            interaction,
            view=self._resolution_view,
            entries_text=str(self.entries.value),
            reason=str(self.reason.value),
        )


def _build_draft_entries(
    parsed: tuple[MatchEntryDraftInput, ...],
    candidate_sets: tuple[MatchEntryCandidateSetDTO, ...],
) -> list[_DraftEntry]:
    if len(parsed) != len(candidate_sets):
        raise ValueError("GameAccount 후보 검색 결과 수가 입력 엔트리와 일치하지 않습니다.")
    result: list[_DraftEntry] = []
    for entry, candidate_set in zip(parsed, candidate_sets, strict=True):
        if candidate_set.query != entry.game_account_query:
            raise ValueError("GameAccount 후보 검색 결과가 원래 입력과 일치하지 않습니다.")
        result.append(
            _DraftEntry(
                query=entry.game_account_query,
                character_name=entry.character_name,
                candidates=candidate_set.candidates,
            )
        )
    return result


def _selected_id(values: tuple[str, ...], allowed_ids: frozenset[int]) -> int:
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError("대상 경기를 하나 선택해야 합니다.")
    value = int(values[0])
    if value not in allowed_ids:
        raise ValueError("선택한 경기가 현재 작업 대상 목록에 없습니다.")
    return value


def _race_select(
    *,
    custom_id: str,
    placeholder: str,
    choices: tuple[AutocompleteChoice, ...],
) -> discord.ui.Select:
    return discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder,
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
        required=True,
    )


def _candidate_label(candidate: MatchEntryGameAccountCandidateDTO) -> str:
    names = [
        value.strip()
        for value in (candidate.nickname, candidate.ingame_name)
        if isinstance(value, str) and value.strip()
    ]
    name = " / ".join(dict.fromkeys(names)) or "이름 없음"
    return f"GA #{candidate.game_account_id} · {name}"


def _candidate_option_label(candidate: MatchEntryGameAccountCandidateDTO) -> str:
    label = _candidate_label(candidate)
    return label if len(label) <= 100 else label[:97] + "..."


def _duplicate_selected_ids(entries: list[_DraftEntry]) -> set[int]:
    seen: set[int] = set()
    duplicates: set[int] = set()
    for entry in entries:
        value = entry.selected_game_account_id
        if value is None:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _optional_text(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None


def _resolution_view(item: discord.ui.Item) -> MatchEntryResolutionView:
    view = item.view
    if not isinstance(view, MatchEntryResolutionView):
        raise RuntimeError("entry resolution component is not attached to its view")
    return view


async def _respond_stale(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.send_message("이미 종료되었거나 처리 중인 엔트리 입력입니다.", ephemeral=True)


async def _edit_original_view(interaction: discord.Interaction, view: MatchEntryResolutionView) -> None:
    try:
        await interaction.edit_original_response(view=view)
    except Exception:
        log_sanitized_exception(
            view.adapter.ports.logger,
            "match entry draft restore failed operation_id=%s",
            view.operation_id,
        )


async def _delete_original_view(interaction: discord.Interaction) -> None:
    try:
        await interaction.delete_original_response()
    except Exception:
        pass
