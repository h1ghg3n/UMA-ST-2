"""Discord UI primitives for the guarded legacy player-link workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from umacircle_bot.services.dtos import PlayerLinkCandidateDTO, PlayerLinkRequestDTO


@dataclass(frozen=True, slots=True)
class PlayerLinkInteractionContext:
    user_id: int
    guild_id: int
    channel_id: int


PlayerLinkRequestSubmitCallback = Callable[[discord.Interaction, dict[str, str | None]], Awaitable[None]]
PlayerLinkRequestSelectionCallback = Callable[[discord.Interaction, int], Awaitable[None]]
PlayerLinkCandidateSelectionCallback = Callable[[discord.Interaction, int], Awaitable[None]]
PlayerLinkApproveCallback = Callable[[discord.Interaction], Awaitable[None]]
PlayerLinkNoteOpenCallback = Callable[[discord.Interaction, str], Awaitable[None]]
PlayerLinkSearchRefineCallback = Callable[[discord.Interaction], Awaitable[None]]
PlayerLinkRevisionOpenCallback = Callable[[discord.Interaction], Awaitable[None]]


class PlayerLinkRequestModal(discord.ui.Modal, title="기존 기록 연결 요청"):
    def __init__(
        self,
        *,
        on_submit_callback: PlayerLinkRequestSubmitCallback,
        title: str = "기존 기록 연결 요청",
        defaults: dict[str, str | None] | None = None,
    ) -> None:
        super().__init__(title=title, timeout=600)
        self._on_submit_callback = on_submit_callback
        values = defaults or {}
        self.ingame_name = discord.ui.TextInput(
            custom_id="player-link-ingame-name",
            label="현재 인게임 닉네임",
            required=True,
            max_length=100,
            default=values.get("ingame_name"),
        )
        self.uma_pid = discord.ui.TextInput(
            custom_id="player-link-uma-pid",
            label="UMA PID",
            placeholder="숫자만 입력",
            required=True,
            min_length=1,
            max_length=32,
            default=values.get("uma_pid"),
        )
        self.nickname_chunk = discord.ui.TextInput(
            custom_id="player-link-nickname-chunk",
            label="기억나는 과거 닉네임 일부",
            required=False,
            max_length=100,
            default=values.get("nickname_chunk"),
        )
        self.participation_hint = discord.ui.TextInput(
            custom_id="player-link-participation-hint",
            label="참가 시기 또는 경기",
            required=False,
            max_length=255,
            default=values.get("participation_hint"),
        )
        self.note = discord.ui.TextInput(
            custom_id="player-link-note",
            label="추가 설명",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=values.get("note"),
        )
        for item in (
            self.ingame_name,
            self.uma_pid,
            self.nickname_chunk,
            self.participation_hint,
            self.note,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_callback(
            interaction,
            {
                "ingame_name": _optional(self.ingame_name.value),
                "uma_pid": _optional(self.uma_pid.value),
                "nickname_chunk": _optional(self.nickname_chunk.value),
                "participation_hint": _optional(self.participation_hint.value),
                "note": _optional(self.note.value),
            },
        )


class PlayerLinkCancelModal(discord.ui.Modal, title="기존 기록 연결 요청 취소"):
    def __init__(self, *, on_submit_callback: PlayerLinkRequestSubmitCallback) -> None:
        super().__init__(timeout=600)
        self.reason = discord.ui.TextInput(
            custom_id="player-link-cancel-reason",
            label="취소 사유",
            required=False,
            max_length=255,
        )
        self.add_item(self.reason)
        self._on_submit_callback = on_submit_callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_callback(
            interaction,
            {
                "reason": _optional(self.reason.value),
            },
        )


class PlayerLinkStaffNoteModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        action: str,
        on_submit_callback: PlayerLinkRequestSubmitCallback,
    ) -> None:
        title = "연결 요청 보류" if action == "review" else "연결 요청 거부"
        super().__init__(title=title, timeout=600)
        self._action = action
        self._on_submit_callback = on_submit_callback
        self.note = discord.ui.TextInput(
            custom_id=f"player-link-{action}-note",
            label="운영 메모",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=255,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_submit_callback(
            interaction,
            {
                "action": self._action,
                "note": _optional(self.note.value),
            },
        )


class PlayerLinkRequestSelectView(discord.ui.View):
    def __init__(
        self,
        *,
        context: PlayerLinkInteractionContext,
        requests: tuple[PlayerLinkRequestDTO, ...],
        on_selected: PlayerLinkRequestSelectionCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.context = context
        self._on_selected = on_selected
        self._allowed_ids = frozenset(request.id for request in requests)
        options = [
            discord.SelectOption(
                label=_truncate(f"#{request.id} {_safe_label(request.discord_nickname_snapshot)}", 100),
                description=_truncate(f"{request.status} · {request.created_at:%Y-%m-%d %H:%M UTC}", 100),
                value=str(request.id),
            )
            for request in requests
        ]
        self.select = discord.ui.Select(
            custom_id="player-link-request-select",
            placeholder="연결 요청 선택",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._select_callback
        self.add_item(self.select)

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        request_id = _selected_id(self.select, self._allowed_ids)
        if request_id is None:
            await interaction.response.send_message("현재 화면의 요청만 선택할 수 있습니다.", ephemeral=True)
            return
        await self._on_selected(interaction, request_id)


class PlayerLinkCandidateSelectView(discord.ui.View):
    def __init__(
        self,
        *,
        context: PlayerLinkInteractionContext,
        candidates: tuple[PlayerLinkCandidateDTO, ...],
        on_selected: PlayerLinkCandidateSelectionCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.context = context
        self._on_selected = on_selected
        self._allowed_ids = frozenset(candidate.game_account_id for candidate in candidates)
        options = [
            discord.SelectOption(
                label=_truncate(_safe_label(candidate.legacy_nickname or candidate.ingame_name or "이름 없음"), 100),
                description=_truncate(" · ".join(candidate.match_reasons), 100),
                value=str(candidate.game_account_id),
            )
            for candidate in candidates
        ]
        self.select = discord.ui.Select(
            custom_id="player-link-candidate-select",
            placeholder="후보 선택",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._select_callback
        self.add_item(self.select)

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        candidate_id = _selected_id(self.select, self._allowed_ids)
        if candidate_id is None:
            await interaction.response.send_message("현재 화면의 후보만 선택할 수 있습니다.", ephemeral=True)
            return
        await self._on_selected(interaction, candidate_id)


class PlayerLinkCandidateDetailView(discord.ui.View):
    def __init__(
        self,
        *,
        context: PlayerLinkInteractionContext,
        on_approve: PlayerLinkApproveCallback,
        on_open_note: PlayerLinkNoteOpenCallback,
        on_refine_search: PlayerLinkSearchRefineCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.context = context
        self._on_approve = on_approve
        self._on_open_note = on_open_note
        self._on_refine_search = on_refine_search

    @discord.ui.button(label="연결 승인", style=discord.ButtonStyle.success, custom_id="player-link-approve")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_approve(interaction)

    @discord.ui.button(label="검색어 수정", style=discord.ButtonStyle.secondary, custom_id="player-link-refine-search")
    async def refine_search(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_refine_search(interaction)

    @discord.ui.button(label="사용자 수정 요청", style=discord.ButtonStyle.secondary, custom_id="player-link-review")
    async def review(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_open_note(interaction, "review")

    @discord.ui.button(label="거부", style=discord.ButtonStyle.danger, custom_id="player-link-reject")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_open_note(interaction, "reject")


class PlayerLinkNoCandidateView(discord.ui.View):
    def __init__(
        self,
        *,
        context: PlayerLinkInteractionContext,
        on_open_note: PlayerLinkNoteOpenCallback,
        on_refine_search: PlayerLinkSearchRefineCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.context = context
        self._on_open_note = on_open_note
        self._on_refine_search = on_refine_search

    @discord.ui.button(
        label="검색어 수정", style=discord.ButtonStyle.secondary, custom_id="player-link-no-candidate-refine"
    )
    async def refine_search(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_refine_search(interaction)

    @discord.ui.button(
        label="사용자 수정 요청", style=discord.ButtonStyle.secondary, custom_id="player-link-no-candidate-review"
    )
    async def review(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_open_note(interaction, "review")

    @discord.ui.button(label="거부", style=discord.ButtonStyle.danger, custom_id="player-link-no-candidate-reject")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_open_note(interaction, "reject")


class PlayerLinkOwnedStatusView(discord.ui.View):
    def __init__(
        self,
        *,
        context: PlayerLinkInteractionContext,
        on_open_revision: PlayerLinkRevisionOpenCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.context = context
        self._on_open_revision = on_open_revision

    @discord.ui.button(label="정보 보충 후 재요청", style=discord.ButtonStyle.primary, custom_id="player-link-revise")
    async def revise(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._on_open_revision(interaction)


def _optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _selected_id(select: discord.ui.Select, allowed_ids: frozenset[int]) -> int | None:
    if len(select.values) != 1:
        return None
    try:
        value = int(select.values[0])
    except (TypeError, ValueError):
        return None
    return value if value in allowed_ids else None


def _safe_label(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").strip()


def _truncate(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else f"{value[: maximum - 1]}…"
