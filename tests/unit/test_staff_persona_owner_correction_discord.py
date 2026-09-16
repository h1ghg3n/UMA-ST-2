"""Discord presentation tests for staff GameAccount owner correction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffGameAccountOwnerCorrectionConfirmView,
    StaffGameAccountOwnerCorrectionDiscordAdapter,
    StaffGameAccountOwnerCorrectionInputModal,
    StaffGameAccountOwnerCorrectionRegionView,
    StaffPersonaInteractionContext,
)
from uma_st2.application.identity import (
    CorrectedGameAccountOwner,
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionPreview,
    StaffGameAccountOwnerCorrectionState,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
TARGET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _preview() -> StaffGameAccountOwnerCorrectionPreview:
    return StaffGameAccountOwnerCorrectionPreview(
        state=StaffGameAccountOwnerCorrectionState(
            guild_id="987",
            game_account_id=71,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            nickname="이동 계정",
            affiliation="소속",
            source_persona_id=SOURCE_ID,
            source_persona_display_name="현재 소유자",
            source_persona_status=PersonaStatus.NORMAL,
            source_has_wallet=True,
            source_game_account_count=2,
            source_qualifying_game_account_count=2,
            target_persona_id=TARGET_ID,
            target_persona_display_name="새 소유자",
            target_persona_status=PersonaStatus.WARNING,
            target_has_wallet=True,
            target_game_account_count=1,
            target_qualifying_game_account_count=1,
        ),
        evidence_reason="운영 기록과 본인 확인 완료",
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.preview = _preview()
        self.calls: list[dict[str, object]] = []

    def get_preview(self, **kwargs: object) -> StaffGameAccountOwnerCorrectionPreview:
        self.calls.append(kwargs)
        return self.preview


class RecordingCommands:
    def __init__(self) -> None:
        self.commands: list[CorrectGameAccountOwner] = []

    def correct(self, command: CorrectGameAccountOwner) -> CorrectedGameAccountOwner:
        self.commands.append(command)
        state = _preview().state
        return CorrectedGameAccountOwner(
            game_account_id=state.game_account_id,
            game_region=state.game_region,
            uma_pid=state.uma_pid,
            nickname=state.nickname,
            affiliation=state.affiliation,
            source_persona_id=state.source_persona_id,
            source_persona_display_name=state.source_persona_display_name,
            source_persona_status=state.source_persona_status,
            source_has_wallet=state.source_has_wallet,
            source_game_account_count=1,
            source_qualifying_game_account_count=1,
            target_persona_id=state.target_persona_id,
            target_persona_display_name=state.target_persona_display_name,
            target_persona_status=state.target_persona_status,
            target_has_wallet=state.target_has_wallet,
            target_game_account_count=2,
            target_qualifying_game_account_count=2,
            evidence_reason="운영 기록과 본인 확인 완료",
            corrected_at=NOW,
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
    StaffGameAccountOwnerCorrectionDiscordAdapter,
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
        StaffGameAccountOwnerCorrectionDiscordAdapter(
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


def test_region_modal_flow_builds_full_zero_write_consequence_preview() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    navigation = RecordingNavigation()

    asyncio.run(
        adapter.show_region(
            interaction,
            navigation=navigation,
            context=context,
            persona_id=TARGET_ID,
            persona_display_name="새 소유자",
            source_view=None,
        )
    )
    region_view = interaction.response.edits[-1]["view"]
    assert isinstance(region_view, StaffGameAccountOwnerCorrectionRegionView)
    region = _select(region_view)
    region._values = [GameRegion.KR.value]  # type: ignore[attr-defined]
    asyncio.run(region.callback(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, StaffGameAccountOwnerCorrectionInputModal)
    assert modal.evidence_reason.required is True
    modal.pid._value = "123456789"  # type: ignore[attr-defined]
    modal.evidence_reason._value = "운영 기록과 본인 확인 완료"  # type: ignore[attr-defined]
    asyncio.run(modal.on_submit(interaction))

    preview_view = interaction.response.edits[-1]["view"]
    assert isinstance(preview_view, StaffGameAccountOwnerCorrectionConfirmView)
    assert region_view.is_finished() is True
    text = _layout_text(preview_view)
    assert "현재 소유자" in text and "새 소유자" in text
    assert "KR · `123456789` · 이동 계정" in text
    assert "2 → 1개" in text and "1 → 2개" in text
    assert "wallet/Circle Point" in text
    assert commands.commands == []
    assert queries.calls == [
        {
            "guild_id": "987",
            "target_persona_id": TARGET_ID,
            "game_region": GameRegion.KR,
            "uma_pid": "123456789",
            "evidence_reason": "운영 기록과 본인 확인 완료",
        }
    ]
    assert authorization.calls == [(interaction, "staff.persona")] * 3


def test_final_confirm_uses_interaction_key_and_private_committed_receipt() -> None:
    adapter, queries, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffGameAccountOwnerCorrectionConfirmView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                preview=queries.preview,
            )
        )
    )

    asyncio.run(adapter.correct(interaction, context=context, preview=queries.preview, source_view=source_view))

    assert preparation.calls == [(interaction, "staff.persona", True)]
    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.expected_source_persona_id == SOURCE_ID
    assert command.expected_state_fingerprint == queries.preview.state.state_fingerprint
    payload = interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    receipt = _layout_text(terminal)
    assert "GameAccount 소유자 정정 완료" in receipt
    assert "••••6789" in receipt and "123456789" not in receipt
    assert "wallet/Circle Point, Rating과 과거 귀속은 변경하지 않았습니다" in receipt
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    source_view = asyncio.run(
        _inline(
            lambda: StaffGameAccountOwnerCorrectionRegionView(
                adapter=adapter,
                navigation=RecordingNavigation(),
                context=context,
                persona_id=TARGET_ID,
                persona_display_name="새 소유자",
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
            persona_id=TARGET_ID,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            evidence_reason="운영 기록과 본인 확인 완료",
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
            persona_id=TARGET_ID,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            evidence_reason="확인",
            source_view=None,
        )
    )
    asyncio.run(adapter.correct(wrong, context=context, preview=queries.preview, source_view=None))

    assert queries.calls == []
    assert commands.commands == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert len(wrong.response.messages) == 2
    assert all(kwargs["ephemeral"] is True for _, kwargs in wrong.response.messages)
