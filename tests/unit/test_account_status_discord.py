"""Discord adapter tests for private Account status navigation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import AccountStatusDiscordAdapter, AccountStatusView
from uma_st2.adapters.discord.strings.account import format_account_overview
from uma_st2.application.identity import (
    AccountMatchHistoryItem,
    AccountMatchHistoryPage,
    AccountMatchSummary,
    AccountStatusOverview,
    AccountWin5HistoryPage,
)
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)


def _overview(*, status: PersonaStatus = PersonaStatus.NORMAL) -> AccountStatusOverview:
    return AccountStatusOverview(
        persona_id="persona-1",
        display_name="테스트 Persona",
        persona_status=status,
        wallet_balance=500,
        game_account_count=1,
        eligible_game_account_count=1,
        registration_request=None,
        match=AccountMatchSummary(
            season_key="2026-split-2",
            season_name="2026 Split 2",
            participated_match_count=1,
            entry_count=1,
            win_count=1,
            top3_count=1,
            average_rank=None,
            excluded_terminal_match_count=0,
        ),
        win5=None,
    )


def _match_page(page: int) -> AccountMatchHistoryPage:
    return AccountMatchHistoryPage(
        season_key="2026-split-2",
        season_name="2026 Split 2",
        page=page,
        total_count=6,
        items=(
            AccountMatchHistoryItem(
                match_id=page + 1,
                match_name=f"경기 {page + 1}",
                scheduled_at=NOW,
                status=MatchStatus.SETTLED,
                source_kind=MatchSourceKind.NATIVE_V2,
                game_account_id=1,
                game_account_nickname="계정",
                character_name="캐릭터",
                entry_number=1,
                rank=1,
                field_size=3,
                rating_delta=None,
            ),
        ),
    )


class RecordingQueries:
    def __init__(self, *, overview: AccountStatusOverview | None = None) -> None:
        self.overview = overview or _overview()
        self.calls: list[tuple[str, int | None]] = []

    def get_overview(self, *, discord_user_id: str, guild_id: str) -> AccountStatusOverview:
        assert discord_user_id == "123"
        assert guild_id == "987"
        self.calls.append(("overview", None))
        return self.overview

    def get_match_history(self, *, discord_user_id: str, page: int = 0) -> AccountMatchHistoryPage:
        assert discord_user_id == "123"
        self.calls.append(("match", page))
        return _match_page(page)

    def get_win5_history(self, *, discord_user_id: str, page: int = 0) -> AccountWin5HistoryPage:
        assert discord_user_id == "123"
        self.calls.append(("win5", page))
        return AccountWin5HistoryPage(season=None, page=page, total_count=0, items=())

    def get_identity_details(self, **kwargs: object) -> object:
        raise AssertionError(f"Unexpected identity query: {kwargs}")


class RecordingPreparation:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return self.allowed


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


class RecordingResponse:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.defers: list[dict[str, object]] = []
        self.done = False

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self.done = True

    def is_done(self) -> bool:
        return self.done


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingInteraction:
    def __init__(
        self,
        *,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
        fail_edit: bool = False,
    ) -> None:
        self.id = 555
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_edit:
            raise RuntimeError("simulated edit failure")
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _track_stop(view: discord.ui.LayoutView) -> list[bool]:
    calls: list[bool] = []
    original_stop = view.stop

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def _adapter() -> tuple[
    AccountStatusDiscordAdapter,
    RecordingQueries,
    RecordingPreparation,
    RecordingAuthorization,
]:
    queries = RecordingQueries()
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        AccountStatusDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            prepare_command=preparation,
            authorize_interaction=authorization,
            run_application=_inline,  # type: ignore[arg-type]
        ),
        queries,
        preparation,
        authorization,
    )


def test_status_command_is_private_and_opens_detached_overview() -> None:
    adapter, queries, preparation, _ = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(adapter.show_status(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "account.status", True)]
    assert queries.calls == [("overview", None)]
    view = interaction.edits[0]["view"]
    assert isinstance(view, AccountStatusView)
    assert _button(view, "개요").disabled is True
    assert _button(view, "룸매치").disabled is False
    assert _button(view, "닫기").style is discord.ButtonStyle.secondary


def test_each_tab_and_page_interaction_reauthorizes_and_runs_a_fresh_query() -> None:
    adapter, queries, _, authorization = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    overview_view = initial.edits[0]["view"]
    assert isinstance(overview_view, AccountStatusView)

    tab_interaction = RecordingInteraction()
    asyncio.run(_button(overview_view, "룸매치").callback(tab_interaction))  # type: ignore[arg-type]
    assert tab_interaction.response.defers == [{"thinking": False}]
    match_view = tab_interaction.edits[0]["view"]
    assert isinstance(match_view, AccountStatusView)
    assert queries.calls == [("overview", None), ("match", 0)]
    assert authorization.calls == [(tab_interaction, "account.status")]
    assert _button(match_view, "다음").disabled is False

    next_interaction = RecordingInteraction()
    asyncio.run(_button(match_view, "다음").callback(next_interaction))  # type: ignore[arg-type]

    assert queries.calls[-1] == ("match", 1)
    assert authorization.calls[-1] == (next_interaction, "account.status")
    assert next_interaction.response.defers == [{"thinking": False}]
    next_view = next_interaction.edits[0]["view"]
    assert isinstance(next_view, AccountStatusView)
    assert _button(next_view, "다음").disabled is True
    assert _button(next_view, "이전").disabled is False


def test_same_message_navigation_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, AccountStatusView)
    interaction = RecordingInteraction(fail_edit=True)
    stop_calls = _track_stop(source_view)
    source_stopped_at_edit: list[bool] = []

    async def fail_edit(**kwargs: object) -> None:
        source_stopped_at_edit.append(bool(stop_calls))
        raise RuntimeError("Discord component edit failed")

    interaction.edit_original_response = fail_edit  # type: ignore[method-assign]

    asyncio.run(_button(source_view, "룸매치").callback(interaction))  # type: ignore[arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert source_view.is_finished() is True
    assert interaction.response.defers == [{"thinking": False}]
    assert interaction.followup.messages[0][0] == (
        "계정 상태 화면을 갱신하지 못했습니다. `/account status`를 다시 열어 주세요."
    )


def test_navigation_query_failure_keeps_source_view_live() -> None:
    adapter, queries, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, AccountStatusView)
    stop_calls = _track_stop(source_view)

    def fail_query(**_: object) -> AccountMatchHistoryPage:
        raise RuntimeError("query failed")

    queries.get_match_history = fail_query  # type: ignore[method-assign]
    interaction = RecordingInteraction()
    asyncio.run(_button(source_view, "룸매치").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages[0][1]["ephemeral"] is True


def test_navigation_authorization_rejection_after_defer_keeps_source_view_live() -> None:
    adapter, queries, _, authorization = _adapter()
    authorization.allowed = False
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, AccountStatusView)
    stop_calls = _track_stop(source_view)
    interaction = RecordingInteraction()

    asyncio.run(_button(source_view, "룸매치").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "account.status")]
    assert queries.calls == [("overview", None)]
    assert stop_calls == []
    assert interaction.edits == []


def test_close_replaces_components_v2_message_with_buttonless_terminal_layout() -> None:
    adapter, _, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, AccountStatusView)
    interaction = RecordingInteraction()

    asyncio.run(_button(source_view, "닫기").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    payload = interaction.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert _layout_text(terminal) == "계정 상태 화면을 닫았습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_close_edit_failure_stops_source_and_sends_reopen_notice() -> None:
    adapter, _, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, AccountStatusView)
    stop_calls = _track_stop(source_view)
    interaction = RecordingInteraction(fail_edit=True)

    asyncio.run(_button(source_view, "닫기").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    assert interaction.followup.messages[0][0] == (
        "계정 상태 화면을 갱신하지 못했습니다. `/account status`를 다시 열어 주세요."
    )


def test_mismatched_opener_context_is_rejected_before_authorization_or_query() -> None:
    adapter, queries, _, authorization = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.show_status(initial))  # type: ignore[arg-type]
    view = initial.edits[0]["view"]
    assert isinstance(view, AccountStatusView)

    mismatch = RecordingInteraction(user_id=999)
    asyncio.run(_button(view, "WIN5").callback(mismatch))  # type: ignore[arg-type]

    assert queries.calls == [("overview", None)]
    assert authorization.calls == []
    assert mismatch.response.messages[0][1]["ephemeral"] is True


def test_pending_approval_overview_explains_read_only_member_state() -> None:
    copy = format_account_overview(_overview(status=PersonaStatus.PENDING_APPROVAL))

    assert "승인 대기" in copy
    assert "새 베팅이나 WIN5 제출" in copy
    assert "/account status" in copy
