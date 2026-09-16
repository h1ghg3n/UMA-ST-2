"""Discord presentation tests for Staff Persona direct access attachment."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffDiscordAttachConfirmView,
    StaffDiscordAttachDiscordAdapter,
    StaffDiscordAttachNoteModal,
    StaffDiscordAttachPickerView,
    StaffPersonaInteractionContext,
)
from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    AttachedDiscordAccount,
    StaffDiscordAttachState,
)
from uma_st2.domain.identity import PersonaStatus

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _preview() -> StaffDiscordAttachState:
    return StaffDiscordAttachState(
        guild_id="987",
        persona_id=PERSONA_ID,
        persona_display_name="기존 Persona",
        persona_status=PersonaStatus.NORMAL,
        target_discord_user_id="123456",
        current_persona_id=None,
        active_registration_request_id=None,
        has_wallet=True,
        qualifying_game_account_count=1,
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.preview = _preview()
        self.calls: list[tuple[str, str, str]] = []

    def get_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
    ) -> StaffDiscordAttachState:
        self.calls.append((guild_id, persona_id, discord_user_id))
        return self.preview


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[AttachDiscordAccountToPersona] = []

    def attach(self, command: AttachDiscordAccountToPersona) -> AttachedDiscordAccount:
        self.commands.append(command)
        preview = _preview()
        return AttachedDiscordAccount(
            persona_id=preview.persona_id,
            persona_display_name=preview.persona_display_name,
            persona_status=preview.persona_status,
            target_discord_user_id=preview.target_discord_user_id,
            has_wallet=preview.has_wallet,
            qualifying_game_account_count=preview.qualifying_game_account_count,
            operational_note=command.operational_note,
            attached_at=NOW,
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
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, object]] = []

    async def show_panel_component(
        self,
        _interaction: object,
        *,
        selected_persona_id: str | None,
        source_view: object,
        **_kwargs: object,
    ) -> None:
        self.calls.append((selected_persona_id, source_view))


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    StaffDiscordAttachDiscordAdapter,
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
        StaffDiscordAttachDiscordAdapter(
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


def _button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def test_member_picker_to_complete_preview_is_private_and_fresh_authorized() -> None:
    adapter, queries, _, _, authorization = _adapter()
    navigation = RecordingNavigation()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.show_picker(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            source_view=None,
        )
    )
    picker = interaction.response.edits[-1]["view"]
    assert isinstance(picker, StaffDiscordAttachPickerView)
    selector = next(item for item in picker.walk_children() if isinstance(item, discord.ui.UserSelect))
    selector._values = [SimpleNamespace(id=123456, display_name="연결 대상")]  # type: ignore[attr-defined]
    asyncio.run(selector.callback(interaction))

    preview_view = interaction.response.edits[-1]["view"]
    assert isinstance(preview_view, StaffDiscordAttachConfirmView)
    assert picker.is_finished() is True
    text = _layout_text(preview_view)
    assert "연결 대상" in text
    assert "기존 Persona" in text
    assert "Wallet: 준비됨" in text
    assert "연결 후 member mutation: 사용 가능" in text
    assert queries.calls == [("987", PERSONA_ID, "123456")]
    assert authorization.calls == [
        (interaction, "staff.persona"),
        (interaction, "staff.persona"),
    ]


def test_optional_note_modal_refreshes_preview_without_mutation() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    navigation = RecordingNavigation()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = queries.preview
    view = asyncio.run(
        _inline(
            lambda: StaffDiscordAttachConfirmView(
                adapter=adapter,
                navigation=navigation,
                context=context,
                preview=preview,
                target_display_name="연결 대상",
                operational_note=None,
            )
        )
    )

    asyncio.run(_button(view, label="메모 입력").callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, StaffDiscordAttachNoteModal)
    assert modal.note.required is False
    modal.note._value = "검수 완료"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    refreshed = interaction.response.edits[-1]["view"]
    assert isinstance(refreshed, StaffDiscordAttachConfirmView)
    assert "검수 완료" in _layout_text(refreshed)
    assert view.is_finished() is True
    assert commands.commands == []
    assert authorization.calls == [
        (interaction, "staff.persona"),
        (interaction, "staff.persona"),
    ]


def test_final_confirm_uses_interaction_key_and_returns_private_committed_receipt() -> None:
    adapter, queries, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffDiscordAttachConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=queries.preview,
                target_display_name="연결 대상",
                operational_note="검수 완료",
            )
        )
    )

    asyncio.run(
        adapter.attach(
            interaction,
            context=context,
            preview=queries.preview,
            operational_note="검수 완료",
            source_view=source_view,
        )
    )

    assert preparation.calls == [(interaction, "staff.persona", True)]
    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.expected_target_fingerprint == queries.preview.state_fingerprint
    assert command.operational_note == "검수 완료"
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    receipt = _layout_text(terminal)
    assert "Discord 계정 연결 완료" in receipt
    assert "Member mutation: 사용 가능" in receipt
    assert "GameAccount, wallet/Circle Point와 과거 Match 귀속은 변경하지 않았습니다" in receipt
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, queries, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffDiscordAttachConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=queries.preview,
                target_display_name="연결 대상",
                operational_note=None,
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
            target_discord_user_id=123456,
            target_display_name="연결 대상",
            operational_note=None,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "Discord 계정 연결 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
    )


def test_wrong_bound_context_never_queries_or_mutates() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    opener = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(opener)
    wrong = RecordingInteraction(channel_id=999)

    asyncio.run(
        adapter.show_preview(
            wrong,
            navigation=RecordingNavigation(),
            context=context,
            persona_id=PERSONA_ID,
            target_discord_user_id=123456,
            target_display_name="연결 대상",
            operational_note=None,
            source_view=None,
        )
    )
    asyncio.run(
        adapter.attach(
            wrong,
            context=context,
            preview=queries.preview,
            operational_note=None,
            source_view=None,
        )
    )

    assert queries.calls == []
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert len(wrong.response.messages) == 2
    assert all(kwargs["ephemeral"] is True for _, kwargs in wrong.response.messages)
