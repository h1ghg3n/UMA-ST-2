"""Discord presentation tests for staff peer GameAccount addition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffGameAccountAddConfirmView,
    StaffGameAccountAddDiscordAdapter,
    StaffGameAccountAddInputModal,
    StaffGameAccountAddRegionView,
    StaffPersonaInteractionContext,
)
from uma_st2.application.identity import (
    AddedGameAccount,
    AddGameAccountToPersona,
    StaffGameAccountAddPreview,
    StaffGameAccountAddState,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _preview() -> StaffGameAccountAddPreview:
    return StaffGameAccountAddPreview(
        state=StaffGameAccountAddState(
            guild_id="987",
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            persona_status=PersonaStatus.WARNING,
            has_wallet=True,
            game_account_count=1,
            qualifying_game_account_count=1,
            game_region=GameRegion.JP,
            uma_pid="123456789",
            registered_game_account_id=None,
            registered_persona_id=None,
        ),
        nickname="새 계정",
        affiliation="소속",
        reason="복수 계정 등록 확인",
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.preview = _preview()
        self.calls: list[dict[str, object]] = []

    def get_preview(self, **kwargs: object) -> StaffGameAccountAddPreview:
        self.calls.append(kwargs)
        return self.preview


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[AddGameAccountToPersona] = []

    def add(self, command: AddGameAccountToPersona) -> AddedGameAccount:
        self.commands.append(command)
        preview = _preview()
        return AddedGameAccount(
            persona_id=preview.state.persona_id,
            persona_display_name=preview.state.persona_display_name,
            persona_status=preview.state.persona_status,
            game_account_id=81,
            game_region=preview.state.game_region,
            uma_pid=preview.state.uma_pid,
            nickname=preview.nickname,
            affiliation=preview.affiliation,
            has_wallet=preview.state.has_wallet,
            game_account_count=2,
            qualifying_game_account_count=2,
            reason=preview.reason,
            added_at=NOW,
        )


class RecordingPreparation:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return True


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


class RecordingResponse:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.modal: discord.ui.Modal | None = None

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modal = modal

    def is_done(self) -> bool:
        return False


class RecordingFollowup:
    async def send(self, *_args: object, **_kwargs: object) -> None:
        return None


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 900,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id, display_name="운영자")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class RecordingNavigation:
    async def show_panel_component(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    StaffGameAccountAddDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingPreparation,
    RecordingAuthorization,
]:
    queries = RecordingQueries()
    commands = RecordingCommands()
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        StaffGameAccountAddDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            prepare_command=preparation,
            authorize_interaction=authorization,
            run_application=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        preparation,
        authorization,
    )


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _select(view: discord.ui.LayoutView) -> discord.ui.Select:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))


def test_region_modal_flow_builds_complete_zero_write_preview() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_region(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            source_view=None,
        )
    )
    region_view = interaction.response.edits[-1]["view"]
    assert isinstance(region_view, StaffGameAccountAddRegionView)
    region = _select(region_view)
    region._values = [GameRegion.JP.value]  # type: ignore[attr-defined]
    asyncio.run(region.callback(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, StaffGameAccountAddInputModal)
    assert modal.reason.required is True
    modal.pid._value = "123456789"  # type: ignore[attr-defined]
    modal.nickname._value = "새 계정"  # type: ignore[attr-defined]
    modal.affiliation._value = "소속"  # type: ignore[attr-defined]
    modal.reason._value = "복수 계정 등록 확인"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview_view = interaction.response.edits[-1]["view"]
    assert isinstance(preview_view, StaffGameAccountAddConfirmView)
    assert region_view.is_finished() is True
    text = _layout_text(preview_view)
    assert "기존 Persona" in text
    assert "JP · `123456789` · 새 계정" in text
    assert "Wallet, Circle Point, 기존 Rating" in text
    assert commands.commands == []
    assert queries.calls == [
        {
            "guild_id": "987",
            "persona_id": PERSONA_ID,
            "game_region": GameRegion.JP,
            "uma_pid": "123456789",
            "nickname": "새 계정",
            "affiliation": "소속",
            "reason": "복수 계정 등록 확인",
        }
    ]
    assert authorization.calls == [(interaction, "staff.persona")] * 3


def test_final_confirm_uses_interaction_key_and_private_committed_receipt() -> None:
    adapter, queries, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffGameAccountAddConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=queries.preview,
            )
        )
    )

    asyncio.run(adapter.add(interaction, context=context, preview=queries.preview, source_view=source_view))

    assert preparation.calls == [(interaction, "staff.persona", True)]
    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.expected_target_fingerprint == queries.preview.state.state_fingerprint
    assert command.reason == "복수 계정 등록 확인"
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    receipt = _layout_text(terminal)
    assert "Peer GameAccount 추가 완료" in receipt
    assert "Account 수: 2개" in receipt
    assert "wallet, Circle Point, Rating과 Match 귀속은 변경하지 않았습니다" in receipt
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffGameAccountAddRegionView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                persona_id=PERSONA_ID,
                persona_display_name="기존 Persona",
            )
        )
    )
    source_stopped_at_edit: list[bool] = []

    async def fail_edit(**_kwargs: object) -> None:
        source_stopped_at_edit.append(source_view.is_finished())
        raise RuntimeError("Discord component edit failed")

    interaction.response.edit_message = fail_edit  # type: ignore[method-assign]

    asyncio.run(
        adapter.show_preview(
            interaction,
            navigation=RecordingNavigation(),
            context=context,
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            game_region=GameRegion.JP,
            uma_pid="123456789",
            nickname="새 계정",
            affiliation="소속",
            reason="복수 계정 등록 확인",
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "계정 관리 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
    )


def test_wrong_bound_context_never_queries_or_mutates() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    context = StaffPersonaInteractionContext.from_interaction(RecordingInteraction())
    wrong = RecordingInteraction(channel_id=999)

    asyncio.run(
        adapter.show_preview(
            wrong,
            navigation=RecordingNavigation(),
            context=context,
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            game_region=GameRegion.JP,
            uma_pid="123456789",
            nickname="새 계정",
            affiliation=None,
            reason="확인",
            source_view=None,
        )
    )
    asyncio.run(adapter.add(wrong, context=context, preview=queries.preview, source_view=None))

    assert queries.calls == []
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert len(wrong.response.messages) == 2
    assert all(kwargs["ephemeral"] is True for _, kwargs in wrong.response.messages)
