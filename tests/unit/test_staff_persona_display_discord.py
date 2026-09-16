"""Discord presentation tests for staff display-information corrections."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffDisplayEditDiscordAdapter,
    StaffDisplayEditScopeView,
    StaffDisplayGameAccountListView,
    StaffGameAccountDisplayConfirmView,
    StaffGameAccountDisplayInputModal,
    StaffPersonaDisplayConfirmView,
    StaffPersonaDisplayInputModal,
    StaffPersonaInteractionContext,
)
from uma_st2.application.identity import (
    StaffDisplayGameAccountPage,
    StaffGameAccountDisplayPreview,
    StaffGameAccountDisplayState,
    StaffPersonaDisplayPreview,
    StaffPersonaDisplayState,
    UpdatedGameAccountDisplayInfo,
    UpdatedPersonaDisplayName,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _persona_state() -> StaffPersonaDisplayState:
    return StaffPersonaDisplayState(
        guild_id="987",
        persona_id=PERSONA_ID,
        display_name="기존 Persona",
        status=PersonaStatus.EXPELLED,
    )


def _account_state() -> StaffGameAccountDisplayState:
    return StaffGameAccountDisplayState(
        guild_id="987",
        persona_id=PERSONA_ID,
        persona_display_name="기존 Persona",
        persona_status=PersonaStatus.EXPELLED,
        game_account_id=71,
        game_region=GameRegion.KR,
        uma_pid="123456789",
        nickname="기존 계정",
        affiliation="기존 소속",
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.persona_state = _persona_state()
        self.account_state = _account_state()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_persona_state(self, **kwargs: object) -> StaffPersonaDisplayState:
        self.calls.append(("persona-state", kwargs))
        return self.persona_state

    def get_persona_preview(self, **kwargs: object) -> StaffPersonaDisplayPreview:
        self.calls.append(("persona-preview", kwargs))
        return StaffPersonaDisplayPreview(
            state=self.persona_state,
            display_name=str(kwargs["display_name"]),
            reason=str(kwargs["reason"]),
        )

    def list_game_accounts(self, **kwargs: object) -> StaffDisplayGameAccountPage:
        self.calls.append(("accounts", kwargs))
        return StaffDisplayGameAccountPage(
            persona=self.persona_state,
            items=(self.account_state,),
            page=int(kwargs["page"]),
            total_count=21,
        )

    def get_game_account_preview(self, **kwargs: object) -> StaffGameAccountDisplayPreview:
        self.calls.append(("account-preview", kwargs))
        return StaffGameAccountDisplayPreview(
            state=self.account_state,
            nickname=str(kwargs["nickname"]),
            affiliation=None if not kwargs["affiliation"] else str(kwargs["affiliation"]),
            reason=str(kwargs["reason"]),
        )


class RecordingCommands:
    def __init__(self) -> None:
        self.persona_commands: list[UpdatePersonaDisplayName] = []
        self.account_commands: list[UpdateGameAccountDisplayInfo] = []

    def update_persona(self, command: UpdatePersonaDisplayName) -> UpdatedPersonaDisplayName:
        self.persona_commands.append(command)
        return UpdatedPersonaDisplayName(
            persona_id=command.target_persona_id,
            previous_display_name="기존 Persona",
            display_name=command.display_name,
            status=PersonaStatus.EXPELLED,
            reason=command.reason,
            updated_at=NOW,
        )

    def update_game_account(self, command: UpdateGameAccountDisplayInfo) -> UpdatedGameAccountDisplayInfo:
        self.account_commands.append(command)
        return UpdatedGameAccountDisplayInfo(
            persona_id=command.target_persona_id,
            persona_display_name="기존 Persona",
            persona_status=PersonaStatus.EXPELLED,
            game_account_id=command.game_account_id,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            previous_nickname="기존 계정",
            nickname=command.nickname,
            previous_affiliation="기존 소속",
            affiliation=command.affiliation,
            reason=command.reason,
            updated_at=NOW,
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
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def show_panel_component(self, *_args: object, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    StaffDisplayEditDiscordAdapter,
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
        StaffDisplayEditDiscordAdapter(
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


def _button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _select(view: discord.ui.LayoutView) -> discord.ui.Select:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_scope_and_persona_modal_build_private_zero_write_preview() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_scope(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            persona_display_name="기존 Persona",
            source_view=None,
        )
    )
    scope = interaction.response.edits[-1]["view"]
    assert isinstance(scope, StaffDisplayEditScopeView)
    assert [item.label for item in scope.walk_children() if isinstance(item, discord.ui.Button)] == [
        "Persona 이름 수정",
        "GameAccount 정보 수정",
        "뒤로",
    ]

    asyncio.run(_button(scope, label="Persona 이름 수정").callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, StaffPersonaDisplayInputModal)
    assert modal.display_name.default == "기존 Persona"
    assert modal.reason.required is True
    modal.display_name._value = "수정 Persona"  # type: ignore[attr-defined]
    modal.reason._value = "표시명 정정"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview = interaction.response.edits[-1]["view"]
    assert isinstance(preview, StaffPersonaDisplayConfirmView)
    assert scope.is_finished() is True
    assert "기존 Persona" in _layout_text(preview)
    assert "수정 Persona" in _layout_text(preview)
    assert commands.persona_commands == commands.account_commands == []
    assert [name for name, _ in queries.calls] == ["persona-state", "persona-preview"]
    assert len(authorization.calls) == 3


def test_game_account_selector_masks_pid_prefills_and_confirms_with_final_key() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_game_accounts(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            page=0,
            source_view=None,
        )
    )
    account_list = interaction.response.edits[-1]["view"]
    assert isinstance(account_list, StaffDisplayGameAccountListView)
    select = _select(account_list)
    assert "••••6789" in select.options[0].label
    assert "123456789" not in select.options[0].label
    assert _button(account_list, label="다음").disabled is False

    select._values = ["71"]  # type: ignore[attr-defined]
    asyncio.run(select.callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, StaffGameAccountDisplayInputModal)
    assert modal.nickname.default == "기존 계정"
    assert modal.affiliation.default == "기존 소속"
    assert modal.reason.required is True
    modal.nickname._value = "수정 계정"  # type: ignore[attr-defined]
    modal.affiliation._value = ""  # type: ignore[attr-defined]
    modal.reason._value = "표시 정정"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview_view = interaction.response.edits[-1]["view"]
    assert isinstance(preview_view, StaffGameAccountDisplayConfirmView)
    assert account_list.is_finished() is True
    assert "PID" not in _layout_text(preview_view) or "123456789" not in _layout_text(preview_view)
    asyncio.run(_button(preview_view, label="계정 정보 수정 확정").callback(interaction))

    command = commands.account_commands[-1]
    assert command.game_account_id == 71
    assert command.nickname == "수정 계정"
    assert command.affiliation is None
    assert command.idempotency_key == "777"
    assert preparation.calls == [(interaction, "staff.persona", True)]
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "GameAccount 정보 수정 완료" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert preview_view.is_finished() is True
    assert [name for name, _ in queries.calls] == ["accounts", "account-preview"]
    assert len(authorization.calls) == 3


def test_persona_final_confirm_uses_preview_fingerprint_and_private_receipt() -> None:
    adapter, _, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=888)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = StaffPersonaDisplayPreview(
        state=_persona_state(),
        display_name="수정 Persona",
        reason="이름 정정",
    )
    source_view = asyncio.run(
        _inline(
            lambda: StaffPersonaDisplayConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=preview,
            )
        )
    )

    asyncio.run(
        adapter.update_persona(
            interaction,
            context=context,
            preview=preview,
            source_view=source_view,
        )
    )

    command = commands.persona_commands[-1]
    assert command.expected_target_fingerprint == preview.state.state_fingerprint
    assert command.idempotency_key == command.correlation_id == "888"
    assert preparation.calls == [(interaction, "staff.persona", True)]
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Persona 이름 수정 완료" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffDisplayEditScopeView(
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
        adapter.show_game_accounts(
            interaction,
            navigation=RecordingNavigation(),
            context=context,
            persona_id=PERSONA_ID,
            page=0,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "계정 관리 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
    )


def test_wrong_opener_context_never_queries_or_mutates() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    original = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(original)
    other = RecordingInteraction(user_id=901)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_game_accounts(
            other,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            page=0,
            source_view=None,
        )
    )

    assert queries.calls == []
    assert commands.persona_commands == commands.account_commands == []
    assert preparation.calls == authorization.calls == []
    assert other.response.messages[-1][0]
