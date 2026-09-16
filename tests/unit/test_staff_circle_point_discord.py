"""Discord interaction tests for staff Circle Point operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffCirclePointConfirmView,
    StaffCirclePointDiscordAdapter,
    StaffCirclePointInputModal,
    StaffPersonaInteractionContext,
)
from uma_st2.application.point import (
    AppliedStaffCirclePoint,
    ApplyStaffCirclePoint,
    StaffCirclePointChoice,
    StaffCirclePointOperation,
    StaffCirclePointPreview,
    StaffCirclePointState,
)
from uma_st2.domain.identity import PersonaStatus

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state() -> StaffCirclePointState:
    return StaffCirclePointState(
        guild_id="987",
        persona_id=PERSONA_ID,
        display_name="포인트 대상",
        status=PersonaStatus.WARNING,
        persona_updated_at=NOW,
        balance=700,
        wallet_updated_at=NOW,
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.preview_calls: list[dict[str, object]] = []

    def search_targets(self, *, query: str, limit: int) -> tuple[StaffCirclePointChoice, ...]:
        self.search_calls.append((query, limit))
        return (
            StaffCirclePointChoice(
                persona_id=PERSONA_ID,
                display_name="포인트 대상",
                status=PersonaStatus.WARNING,
                wallet_available=True,
            ),
        )

    def get_preview(self, **kwargs: object) -> StaffCirclePointPreview:
        self.preview_calls.append(dict(kwargs))
        amount = int(kwargs["amount"])
        return StaffCirclePointPreview(
            state=_state(),
            operation=StaffCirclePointOperation(str(kwargs["operation"])),
            amount=amount,
            reason=str(kwargs["reason"]),
            resulting_balance=700 + amount,
        )


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[ApplyStaffCirclePoint] = []

    def apply(self, command: ApplyStaffCirclePoint) -> AppliedStaffCirclePoint:
        self.commands.append(command)
        return AppliedStaffCirclePoint(
            transaction_id=81,
            persona_id=command.target_persona_id,
            display_name="포인트 대상",
            status=PersonaStatus.WARNING,
            operation=command.operation,
            action=command.operation.point_action,
            amount=command.amount,
            current_balance=700 + command.amount,
            reason=command.reason,
            created_at=NOW,
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
        self.messages: list[tuple[str | None, dict[str, object]]] = []
        self.modal: discord.ui.Modal | None = None

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
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


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    StaffCirclePointDiscordAdapter,
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
        StaffCirclePointDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            authorize_autocomplete=authorization,
            authorize_interaction=authorization,
            prepare_command=preparation,
            run_application=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        preparation,
        authorization,
    )


def _button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_autocomplete_and_initial_modal_use_current_staff_authorization() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction()

    choices = asyncio.run(
        adapter.target_autocomplete(
            interaction,
            "포인트",
            operation=StaffCirclePointOperation.GRANT,
        )
    )
    asyncio.run(
        adapter.open_modal(
            interaction,
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.GRANT,
        )
    )

    assert queries.search_calls == [("포인트", 25)]
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == [
        (interaction, "staff.grant-circle-points"),
        (interaction, "staff.grant-circle-points"),
    ]
    assert choices[0].value == PERSONA_ID
    assert "wallet 있음" in choices[0].name
    modal = interaction.response.modal
    assert isinstance(modal, StaffCirclePointInputModal)
    assert modal.amount.required is True
    assert modal.reason.required is True


def test_modal_preview_is_private_zero_write_and_final_confirm_uses_final_interaction_key() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    modal_interaction = RecordingInteraction(interaction_id=556)
    context = StaffPersonaInteractionContext.from_interaction(modal_interaction)

    asyncio.run(
        adapter.show_preview(
            modal_interaction,
            context=context,
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.ADJUSTMENT,
            amount="-100",
            reason="중복 지급 정정",
            source_view=None,
        )
    )

    assert len(queries.preview_calls) == 1
    assert commands.commands == []
    assert modal_interaction.response.messages[0][1]["ephemeral"] is True
    view = modal_interaction.response.messages[0][1]["view"]
    assert isinstance(view, StaffCirclePointConfirmView)
    assert "현재 잔액: `700`" in _layout_text(view)
    assert "변경 후 잔액: `600`" in _layout_text(view)

    final_interaction = RecordingInteraction(interaction_id=777)
    asyncio.run(_button(view, label="조정 확정").callback(final_interaction))

    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.target_persona_id == PERSONA_ID
    assert command.amount == -100
    assert command.reason == "중복 지급 정정"
    assert command.idempotency_key == "777"
    assert command.correlation_id == "777"
    assert command.actor_discord_user_id == "900"
    assert preparation.calls == [(final_interaction, "staff.adjust-circle-points", True)]
    assert authorization.calls == [(modal_interaction, "staff.adjust-circle-points")]
    payload = final_interaction.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "서클 포인트 운영 조정 완료" in _layout_text(terminal)
    assert "현재 잔액: `600`" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert view.is_finished() is True


def test_final_delivery_failure_still_closes_source_after_application_attempt() -> None:
    adapter, _queries, commands, preparation, _authorization = _adapter()
    interaction = RecordingInteraction(interaction_id=778)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = StaffCirclePointPreview(
        state=_state(),
        operation=StaffCirclePointOperation.GRANT,
        amount=25,
        reason="수동 지급",
        resulting_balance=725,
    )
    source_view = asyncio.run(
        _inline(lambda: StaffCirclePointConfirmView(adapter=adapter, context=context, preview=preview))
    )

    async def fail_delivery(**_kwargs: object) -> None:
        raise RuntimeError("delivery failed")

    interaction.edit_original_response = fail_delivery  # type: ignore[method-assign]

    asyncio.run(
        adapter.apply(
            interaction,
            context=context,
            preview=preview,
            source_view=source_view,
        )
    )

    assert len(commands.commands) == 1
    assert preparation.calls == [(interaction, "staff.grant-circle-points", True)]
    assert source_view.is_finished() is True


def test_edit_prefills_draft_and_cancel_does_not_call_command() -> None:
    adapter, _queries, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = StaffCirclePointPreview(
        state=_state(),
        operation=StaffCirclePointOperation.GRANT,
        amount=25,
        reason="수동 지급",
        resulting_balance=725,
    )
    view = asyncio.run(_inline(lambda: StaffCirclePointConfirmView(adapter=adapter, context=context, preview=preview)))

    asyncio.run(_button(view, label="입력 수정").callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, StaffCirclePointInputModal)
    assert str(modal.amount.default) == "25"
    assert str(modal.reason.default) == "수동 지급"
    assert view.is_finished() is False

    cancel_interaction = RecordingInteraction(interaction_id=888)
    cancel_view = asyncio.run(
        _inline(lambda: StaffCirclePointConfirmView(adapter=adapter, context=context, preview=preview))
    )
    asyncio.run(_button(cancel_view, label="취소").callback(cancel_interaction))

    assert commands.commands == []
    assert preparation.calls == []
    assert [command for _, command in authorization.calls] == [
        "staff.grant-circle-points",
        "staff.grant-circle-points",
    ]
    payload = cancel_interaction.response.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert _layout_text(terminal) == "서클 포인트 변경을 취소했습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert cancel_view.is_finished() is True


def test_same_message_preview_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _queries, commands, preparation, _authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    preview = StaffCirclePointPreview(
        state=_state(),
        operation=StaffCirclePointOperation.GRANT,
        amount=25,
        reason="수동 지급",
        resulting_balance=725,
    )
    source_view = asyncio.run(
        _inline(lambda: StaffCirclePointConfirmView(adapter=adapter, context=context, preview=preview))
    )
    source_stopped_at_edit: list[bool] = []

    async def fail_edit(**_kwargs: object) -> None:
        source_stopped_at_edit.append(source_view.is_finished())
        raise RuntimeError("delivery failed")

    interaction.response.edit_message = fail_edit  # type: ignore[method-assign]

    asyncio.run(
        adapter.show_preview(
            interaction,
            context=context,
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.GRANT,
            amount="30",
            reason="수정 지급",
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert source_view.is_finished() is True
    assert commands.commands == []
    assert preparation.calls == []
    assert interaction.response.messages[-1][0] == (
        "서클 포인트 화면을 갱신하지 못했습니다. 처음 실행한 `/staff` 포인트 명령을 다시 실행해 주세요."
    )


def test_opener_context_mismatch_is_rejected_before_query_or_mutation() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    opener = RecordingInteraction()
    foreign = RecordingInteraction(user_id=901)
    context = StaffPersonaInteractionContext.from_interaction(opener)

    asyncio.run(
        adapter.show_preview(
            foreign,
            context=context,
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.GRANT,
            amount="10",
            reason="권한 검증",
            source_view=None,
        )
    )

    assert queries.preview_calls == []
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert foreign.response.messages[0][0] == "이 화면을 연 운영자와 서버·채널에서만 계속할 수 있습니다."
