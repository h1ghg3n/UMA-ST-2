"""Discord presentation tests for staff direct account registration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffDirectRegistrationConfirmView,
    StaffDirectRegistrationDiscordAdapter,
    StaffDirectRegistrationInputModal,
    StaffDirectRegistrationPickerView,
    StaffDirectRegistrationRegionView,
    StaffPersonaInteractionContext,
)
from uma_st2.application.identity import (
    DirectlyRegisterDiscordAccount,
    DirectlyRegisteredAccount,
    StaffDirectRegistrationPreview,
    StaffDirectRegistrationState,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _preview() -> StaffDirectRegistrationPreview:
    return StaffDirectRegistrationPreview(
        state=StaffDirectRegistrationState(
            guild_id="987",
            target_discord_user_id="123456",
            game_region=GameRegion.KR,
            uma_pid="123456789",
            current_persona_id=None,
            active_registration_request_id=None,
            registered_game_account_id=None,
        ),
        target_display_name_snapshot="등록 대상",
        nickname="게임 닉네임",
        affiliation="소속",
        operational_note="운영 확인 완료",
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.preview = _preview()
        self.calls: list[dict[str, object]] = []

    def get_preview(self, **kwargs: object) -> StaffDirectRegistrationPreview:
        self.calls.append(kwargs)
        return self.preview


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[DirectlyRegisterDiscordAccount] = []

    def register(self, command: DirectlyRegisterDiscordAccount) -> DirectlyRegisteredAccount:
        self.commands.append(command)
        preview = _preview()
        return DirectlyRegisteredAccount(
            target_discord_user_id=preview.state.target_discord_user_id,
            persona_id=PERSONA_ID,
            persona_display_name=preview.target_display_name_snapshot,
            persona_status=PersonaStatus.NORMAL,
            game_account_id=81,
            game_region=preview.state.game_region,
            uma_pid=preview.state.uma_pid,
            nickname=preview.nickname,
            affiliation=preview.affiliation,
            wallet_balance=500,
            initial_grant_amount=500,
            operational_note=preview.operational_note,
            registered_at=NOW,
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
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


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
    StaffDirectRegistrationDiscordAdapter,
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
        StaffDirectRegistrationDiscordAdapter(
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


def _select(view: discord.ui.LayoutView, select_type: type[discord.ui.Select]) -> discord.ui.Select:
    return next(item for item in view.walk_children() if isinstance(item, select_type))


def test_member_region_modal_flow_builds_complete_zero_write_preview() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    navigation = RecordingNavigation()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.show_picker(
            interaction,
            navigation=navigation,
            context=context,
            selected_persona_id=None,
            source_view=None,
        )
    )
    picker = interaction.response.edits[-1]["view"]
    assert isinstance(picker, StaffDirectRegistrationPickerView)
    member = _select(picker, discord.ui.UserSelect)
    member._values = [SimpleNamespace(id=123456, display_name="등록 대상")]  # type: ignore[attr-defined]
    asyncio.run(member.callback(interaction))

    region_view = interaction.response.edits[-1]["view"]
    assert isinstance(region_view, StaffDirectRegistrationRegionView)
    assert picker.is_finished() is True
    region = _select(region_view, discord.ui.Select)
    region._values = [GameRegion.KR.value]  # type: ignore[attr-defined]
    asyncio.run(region.callback(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, StaffDirectRegistrationInputModal)
    modal.pid._value = "123456789"  # type: ignore[attr-defined]
    modal.nickname._value = "게임 닉네임"  # type: ignore[attr-defined]
    modal.affiliation._value = "소속"  # type: ignore[attr-defined]
    modal.note._value = "운영 확인 완료"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview_view = interaction.response.edits[-1]["view"]
    assert isinstance(preview_view, StaffDirectRegistrationConfirmView)
    assert region_view.is_finished() is True
    text = _layout_text(preview_view)
    assert "등록 대상" in text
    assert "KR · `123456789` · 게임 닉네임" in text
    assert "initial_grant +500" in text
    assert commands.commands == []
    assert queries.calls == [
        {
            "guild_id": "987",
            "target_discord_user_id": "123456",
            "target_display_name_snapshot": "등록 대상",
            "game_region": GameRegion.KR,
            "uma_pid": "123456789",
            "nickname": "게임 닉네임",
            "affiliation": "소속",
            "operational_note": "운영 확인 완료",
        }
    ]
    assert authorization.calls == [(interaction, "staff.persona")] * 4


def test_final_confirm_uses_interaction_key_and_private_committed_receipt() -> None:
    adapter, queries, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffDirectRegistrationConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                selected_persona_id=None,
                target_display_name=queries.preview.target_display_name_snapshot,
                preview=queries.preview,
            )
        )
    )

    asyncio.run(
        adapter.register(
            interaction,
            context=context,
            preview=queries.preview,
            source_view=source_view,
        )
    )

    assert preparation.calls == [(interaction, "staff.persona", True)]
    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.expected_target_fingerprint == queries.preview.state.state_fingerprint
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    receipt = _layout_text(terminal)
    assert "신규 직접 등록 완료" in receipt
    assert "Circle Point: +500 · 현재 500" in receipt
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffDirectRegistrationPickerView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                selected_persona_id=None,
            )
        )
    )
    source_stopped_at_edit: list[bool] = []

    async def fail_edit(**_kwargs: object) -> None:
        source_stopped_at_edit.append(source_view.is_finished())
        raise RuntimeError("Discord component edit failed")

    interaction.response.edit_message = fail_edit  # type: ignore[method-assign]

    asyncio.run(
        adapter.show_region(
            interaction,
            navigation=RecordingNavigation(),
            context=context,
            selected_persona_id=None,
            target_discord_user_id=123456,
            target_display_name="등록 대상",
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "직접 등록 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
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
            selected_persona_id=None,
            target_discord_user_id=123456,
            target_display_name="등록 대상",
            game_region=GameRegion.KR,
            uma_pid="123456789",
            nickname="게임 닉네임",
            affiliation=None,
            operational_note=None,
            source_view=None,
        )
    )
    asyncio.run(adapter.register(wrong, context=context, preview=queries.preview, source_view=None))

    assert queries.calls == []
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert len(wrong.response.messages) == 2
    assert all(kwargs["ephemeral"] is True for _, kwargs in wrong.response.messages)
