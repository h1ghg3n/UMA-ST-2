"""Discord presentation tests for staff Persona status management."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffPersonaInteractionContext,
    StaffPersonaStatusConfirmView,
    StaffPersonaStatusDiscordAdapter,
    StaffPersonaStatusReasonModal,
    StaffPersonaStatusView,
)
from uma_st2.application.identity import (
    ChangedPersonaStatus,
    ChangePersonaStatus,
    StaffPersonaStatusPreview,
    StaffPersonaStatusState,
)
from uma_st2.domain.identity import PersonaStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state(status: PersonaStatus = PersonaStatus.NORMAL) -> StaffPersonaStatusState:
    return StaffPersonaStatusState(
        guild_id="987",
        persona_id=PERSONA_ID,
        display_name="상태 대상",
        status=status,
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.state = _state()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_state(self, **kwargs: object) -> StaffPersonaStatusState:
        self.calls.append(("state", kwargs))
        return self.state

    def get_preview(self, **kwargs: object) -> StaffPersonaStatusPreview:
        self.calls.append(("preview", kwargs))
        return StaffPersonaStatusPreview(
            state=self.state,
            desired_status=PersonaStatus(str(kwargs["desired_status"])),
            reason=str(kwargs["reason"]),
        )


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[ChangePersonaStatus] = []

    def change(self, command: ChangePersonaStatus) -> ChangedPersonaStatus:
        self.commands.append(command)
        return ChangedPersonaStatus(
            persona_id=command.target_persona_id,
            display_name="상태 대상",
            previous_status=PersonaStatus.NORMAL,
            status=command.desired_status,
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
    async def show_panel_component(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    StaffPersonaStatusDiscordAdapter,
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
        StaffPersonaStatusDiscordAdapter(
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


def test_status_select_reason_and_preview_are_private_zero_write_steps() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_status(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=PERSONA_ID,
            source_view=None,
        )
    )
    view = interaction.response.edits[-1]["view"]
    assert isinstance(view, StaffPersonaStatusView)
    select = _select(view)
    assert [option.value for option in select.options] == [
        "warning",
        "pending_approval",
        "expelled",
        "withdrawn",
    ]
    assert "현재 상태: 정상" in _layout_text(view)

    select._values = ["pending_approval"]  # type: ignore[attr-defined]
    asyncio.run(select.callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, StaffPersonaStatusReasonModal)
    assert modal.reason.required is True
    modal.reason._value = "추가 승인 필요"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview = interaction.response.edits[-1]["view"]
    assert isinstance(preview, StaffPersonaStatusConfirmView)
    assert view.is_finished() is True
    assert "정상 (`normal`)" in _layout_text(preview)
    assert "승인 대기 (`pending_approval`)" in _layout_text(preview)
    assert "새 Match Bet/WIN5 mutation" in _layout_text(preview)
    assert _button(preview, label="상태 변경 확정").style is discord.ButtonStyle.danger
    assert commands.commands == []
    assert [name for name, _ in queries.calls] == ["state", "preview"]
    assert preparation.calls == []
    assert len(authorization.calls) == 3


def test_status_final_confirm_uses_preview_fingerprint_and_private_receipt() -> None:
    adapter, _, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = StaffPersonaStatusPreview(
        state=_state(),
        desired_status=PersonaStatus.WARNING,
        reason="주의 표시",
    )
    source_view = asyncio.run(
        _inline(
            lambda: StaffPersonaStatusConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=preview,
            )
        )
    )

    asyncio.run(
        adapter.change_status(
            interaction,
            context=context,
            preview=preview,
            source_view=source_view,
        )
    )

    command = commands.commands[-1]
    assert command.expected_target_fingerprint == preview.state.state_fingerprint
    assert command.desired_status is PersonaStatus.WARNING
    assert command.idempotency_key == command.correlation_id == "777"
    assert preparation.calls == [(interaction, "staff.persona", True)]
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Persona 상태 변경 완료" in _layout_text(terminal)
    assert "wallet과 PID 등록 GameAccount가 계속 필요" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffPersonaStatusView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                state=_state(),
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
            desired_status=PersonaStatus.WARNING,
            reason="주의 표시",
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "계정 관리 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
    )


def test_terminal_reinstatement_uses_success_confirmation_style() -> None:
    adapter, _, _, _, _ = _adapter()
    context = StaffPersonaInteractionContext.from_interaction(RecordingInteraction())
    preview = StaffPersonaStatusPreview(
        state=_state(PersonaStatus.EXPELLED),
        desired_status=PersonaStatus.NORMAL,
        reason="복귀 승인",
    )
    view = StaffPersonaStatusConfirmView(
        adapter=adapter,
        navigation=RecordingNavigation(),
        context=context,
        preview=preview,
    )

    assert _button(view, label="상태 변경 확정").style is discord.ButtonStyle.success
    assert "실제 사용에는 wallet" in _layout_text(view)


def test_wrong_opener_context_never_queries_or_mutates() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    original = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(original)
    other = RecordingInteraction(user_id=901)

    asyncio.run(
        adapter.show_status(
            other,
            navigation=RecordingNavigation(),
            context=context,
            persona_id=PERSONA_ID,
            source_view=None,
        )
    )

    assert queries.calls == []
    assert commands.commands == []
    assert preparation.calls == authorization.calls == []
    assert other.response.messages[-1][0]
