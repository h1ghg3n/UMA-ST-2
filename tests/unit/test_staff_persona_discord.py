"""Private Discord Staff Persona registration-review workflow tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    StaffCommandGroup,
    StaffPersonaDiscordAdapter,
    StaffPersonaInteractionContext,
    StaffPersonaPanelView,
    StaffRegistrationApprovalConfirmView,
    StaffRegistrationRejectionModal,
    StaffRegistrationRequestDetailView,
    StaffRegistrationRequestListView,
)
from uma_st2.application.identity import (
    ApproveAccountRegistrationRequest,
    ApprovedAccountRegistration,
    RejectAccountRegistrationRequest,
    RejectedAccountRegistration,
    StaffPersonaChoice,
    StaffPersonaContext,
    StaffPersonaPanel,
    StaffRegistrationRequestPage,
    StaffRegistrationRequestState,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _request() -> StaffRegistrationRequestState:
    return StaffRegistrationRequestState(
        request_id=51,
        discord_account_id=7,
        guild_id="987",
        requester_discord_user_id="123",
        discord_display_name_snapshot="Discord 표시명",
        game_region=GameRegion.KR,
        uma_pid="123456789",
        nickname="게임 닉네임",
        affiliation="소속",
        status=RegistrationRequestStatus.PENDING,
        active_marker=True,
        reason=None,
        created_at=NOW,
        resolved_at=None,
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.request = _request()
        self.panel_calls: list[tuple[str, str | None]] = []
        self.list_calls: list[tuple[str, int]] = []
        self.detail_calls: list[tuple[str, int]] = []
        self.search_calls: list[tuple[str, int]] = []

    def search_personas(self, *, query: str, limit: int) -> tuple[StaffPersonaChoice, ...]:
        self.search_calls.append((query, limit))
        return (
            StaffPersonaChoice(
                persona_id="11111111-2222-4333-8444-555555555555",
                display_name="Persona 이름",
            ),
        )

    def get_panel(self, *, guild_id: str, persona_id: str | None) -> StaffPersonaPanel:
        self.panel_calls.append((guild_id, persona_id))
        return StaffPersonaPanel(pending_request_count=1, selected_persona=None)

    def list_pending_requests(self, *, guild_id: str, page: int) -> StaffRegistrationRequestPage:
        self.list_calls.append((guild_id, page))
        return StaffRegistrationRequestPage(page=page, total_count=1, items=(self.request,))

    def get_pending_request(self, *, guild_id: str, request_id: int) -> StaffRegistrationRequestState:
        self.detail_calls.append((guild_id, request_id))
        return self.request


class RecordingCommands:
    def __init__(self) -> None:
        self.approvals: list[ApproveAccountRegistrationRequest] = []
        self.rejections: list[RejectAccountRegistrationRequest] = []

    def approve(self, command: ApproveAccountRegistrationRequest) -> ApprovedAccountRegistration:
        self.approvals.append(command)
        request = _request()
        return ApprovedAccountRegistration(
            request_id=request.request_id,
            requester_discord_user_id=request.requester_discord_user_id,
            persona_id="11111111-2222-4333-8444-555555555555",
            persona_display_name=request.discord_display_name_snapshot,
            persona_status=PersonaStatus.NORMAL,
            game_account_id=81,
            game_region=request.game_region,
            uma_pid=request.uma_pid,
            nickname=request.nickname,
            affiliation=request.affiliation,
            wallet_balance=500,
            initial_grant_amount=500,
            resolved_at=NOW,
        )

    def reject(self, command: RejectAccountRegistrationRequest) -> RejectedAccountRegistration:
        self.rejections.append(command)
        return RejectedAccountRegistration(
            request_id=command.request_id,
            requester_discord_user_id="123",
            reason=command.reason,
            resolved_at=NOW,
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
        self.defers: list[dict[str, object]] = []

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modal = modal

    def is_done(self) -> bool:
        return bool(self.defers or self.edits or self.messages or self.modal)


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
    StaffPersonaDiscordAdapter,
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
        StaffPersonaDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            attach_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            registration_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            game_account_add_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            owner_correction_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            display_edit_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            status_adapter=SimpleNamespace(),  # type: ignore[arg-type]
            prepare_command=preparation,
            authorize_autocomplete=authorization,
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


def test_staff_command_group_exposes_only_optional_persona_workflow() -> None:
    group = StaffCommandGroup(  # type: ignore[arg-type]
        persona_adapter=SimpleNamespace(),
        circle_point_adapter=SimpleNamespace(),
    )

    assert group.name == "staff"
    assert [command.name for command in group.commands] == [
        "persona",
        "grant-circle-points",
        "adjust-circle-points",
    ]
    command = next(command for command in group.commands if command.name == "persona")
    assert [parameter.name for parameter in command.parameters] == ["persona_id"]
    assert command.parameters[0].required is False


def test_open_panel_and_autocomplete_are_private_and_staff_authorized() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.persona_autocomplete(interaction, "Persona"))
    asyncio.run(adapter.open_panel(interaction, persona_id=None))

    assert commands.approvals == commands.rejections == []
    assert queries.search_calls == [("Persona", 25)]
    assert queries.panel_calls == [("987", None)]
    assert authorization.calls == [(interaction, "staff.persona")]
    assert preparation.calls == [(interaction, "staff.persona", True)]
    assert choices[0].value == "11111111-2222-4333-8444-555555555555"
    view = interaction.edits[0]["view"]
    assert isinstance(view, StaffPersonaPanelView)
    assert [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)] == [
        "등록 요청 검토",
        "신규 직접 등록",
        "Discord 계정 연결",
        "GameAccount 추가",
        "표시 정보 수정",
        "상태 관리",
        "GameAccount 소유자 정정",
        "닫기",
    ]
    attach = _button(view, label="Discord 계정 연결")
    game_account_add = _button(view, label="GameAccount 추가")
    display_edit = _button(view, label="표시 정보 수정")
    status = _button(view, label="상태 관리")
    owner_correction = _button(view, label="GameAccount 소유자 정정")
    assert attach.disabled is True
    assert game_account_add.disabled is True
    assert display_edit.disabled is True
    assert status.disabled is True
    assert owner_correction.disabled is True


def test_selected_persona_actions_are_enabled_only_with_selected_context() -> None:
    adapter, _, _, _, _ = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)
    selected = StaffPersonaContext(
        persona_id="11111111-2222-4333-8444-555555555555",
        display_name="기존 Persona",
        status=PersonaStatus.WARNING,
        game_account_count=2,
    )

    view = StaffPersonaPanelView(
        adapter=adapter,
        context=context,
        panel=StaffPersonaPanel(pending_request_count=0, selected_persona=selected),
        selected_persona_id=selected.persona_id,
    )

    attach = _button(view, label="Discord 계정 연결")
    game_account_add = _button(view, label="GameAccount 추가")
    display_edit = _button(view, label="표시 정보 수정")
    status = _button(view, label="상태 관리")
    owner_correction = _button(view, label="GameAccount 소유자 정정")
    assert attach.disabled is False
    assert game_account_add.disabled is False
    assert display_edit.disabled is False
    assert status.disabled is False
    assert owner_correction.disabled is False
    assert "기존 Persona" in _layout_text(view)


def test_request_list_masks_pid_but_private_detail_shows_full_pid() -> None:
    adapter, queries, _, _, authorization = _adapter()
    interaction = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.show_request_list(
            interaction,
            context=context,
            selected_persona_id=None,
            page=0,
            source_view=None,
        )
    )
    list_view = interaction.response.edits[-1]["view"]
    assert isinstance(list_view, StaffRegistrationRequestListView)
    list_text = _layout_text(list_view)
    assert "••••6789" in list_text
    assert "123456789" not in list_text

    asyncio.run(
        adapter.show_request_detail(
            interaction,
            context=context,
            selected_persona_id=None,
            page=0,
            request_id=51,
            source_view=list_view,
        )
    )
    detail_view = interaction.response.edits[-1]["view"]
    assert isinstance(detail_view, StaffRegistrationRequestDetailView)
    assert "123456789" in _layout_text(detail_view)
    assert queries.list_calls == [("987", 0)]
    assert queries.detail_calls == [("987", 51)]
    assert authorization.calls == [
        (interaction, "staff.persona"),
        (interaction, "staff.persona"),
    ]
    assert list_view.is_finished() is True


def test_same_message_transition_stops_source_before_edit_and_recovers_failure() -> None:
    adapter, _, _, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.open_panel(initial, persona_id=None))
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, StaffPersonaPanelView)
    interaction = RecordingInteraction()
    source_stopped_at_edit: list[bool] = []

    async def fail_edit(**kwargs: object) -> None:
        source_stopped_at_edit.append(source_view.is_finished())
        raise RuntimeError("Discord component edit failed")

    interaction.response.edit_message = fail_edit  # type: ignore[method-assign]

    asyncio.run(
        adapter.show_request_list(
            interaction,
            context=StaffPersonaInteractionContext.from_interaction(initial),
            selected_persona_id=None,
            page=0,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert interaction.response.messages[0][0] == (
        "계정 관리 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
    )


def test_close_replaces_components_v2_message_with_buttonless_terminal_layout() -> None:
    adapter, _, _, _, _ = _adapter()
    initial = RecordingInteraction()
    asyncio.run(adapter.open_panel(initial, persona_id=None))
    source_view = initial.edits[0]["view"]
    assert isinstance(source_view, StaffPersonaPanelView)
    interaction = RecordingInteraction()

    asyncio.run(_button(source_view, label="닫기").callback(interaction))  # type: ignore[arg-type]

    payload = interaction.response.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert _layout_text(terminal) == "Staff Persona 화면을 닫았습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert source_view.is_finished() is True


def test_approval_reloads_preview_and_final_uses_interaction_key() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    preview_interaction = RecordingInteraction(interaction_id=600)
    context = StaffPersonaInteractionContext.from_interaction(preview_interaction)

    asyncio.run(
        adapter.show_approval_confirmation(
            preview_interaction,
            context=context,
            selected_persona_id="context-persona",
            page=0,
            request_id=51,
            source_view=None,
        )
    )
    view = preview_interaction.response.edits[-1]["view"]
    assert isinstance(view, StaffRegistrationApprovalConfirmView)
    assert "••••6789" in _layout_text(view)
    assert "123456789" not in _layout_text(view)

    final_interaction = RecordingInteraction(interaction_id=777)
    final_context = StaffPersonaInteractionContext.from_interaction(final_interaction)
    asyncio.run(
        adapter.approve(
            final_interaction,
            context=final_context,
            request=queries.request,
            source_view=view,
        )
    )

    assert authorization.calls == [
        (preview_interaction, "staff.persona"),
        (final_interaction, "staff.persona"),
    ]
    assert preparation.calls == []
    assert final_interaction.response.defers == [{"thinking": False}]
    assert len(commands.approvals) == 1
    command = commands.approvals[0]
    assert command.expected_request_fingerprint == queries.request.state_fingerprint
    assert command.idempotency_key == command.correlation_id == "777"
    payload = final_interaction.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Circle Point: +500" in _layout_text(terminal)
    assert "123456789" not in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert view.is_finished() is True


def test_rejection_requires_reason_and_wrong_context_never_mutates() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    opener = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(opener)
    source_view = StaffRegistrationRequestDetailView(
        adapter=adapter,
        context=context,
        selected_persona_id=None,
        page=0,
        request=queries.request,
    )

    asyncio.run(
        adapter.open_rejection_modal(
            opener,
            context=context,
            request=queries.request,
            source_view=source_view,
        )
    )
    assert isinstance(opener.response.modal, StaffRegistrationRejectionModal)
    modal = opener.response.modal
    assert modal is not None
    assert modal.reason.min_length == 1
    assert modal._source_view is source_view  # noqa: SLF001
    assert authorization.calls == [(opener, "staff.persona")]
    stop_calls: list[bool] = []
    original_stop = source_view.stop

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    source_view.stop = record_stop  # type: ignore[method-assign]

    final = RecordingInteraction(interaction_id=888)
    final_context = StaffPersonaInteractionContext.from_interaction(final)
    asyncio.run(
        adapter.reject(
            final,
            context=final_context,
            request=queries.request,
            reason="PID 확인 불가",
            source_view=source_view,
        )
    )
    assert preparation.calls == []
    assert final.response.defers == [{"thinking": False}]
    assert len(commands.rejections) == 1
    assert commands.rejections[0].reason == "PID 확인 불가"
    assert commands.rejections[0].idempotency_key == "888"
    payload = final.edits[-1]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Persona, GameAccount, wallet" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert stop_calls == [True]

    wrong = RecordingInteraction(channel_id=999)
    asyncio.run(
        adapter.approve(
            wrong,
            context=context,
            request=replace(queries.request),
            source_view=None,
        )
    )
    assert len(commands.approvals) == 0
    assert preparation.calls == []
    assert wrong.response.messages[0][1]["ephemeral"] is True


def test_duplicate_final_claim_is_terminal_and_never_repeats_mutation() -> None:
    adapter, queries, commands, _, authorization = _adapter()
    opener = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(opener)
    source_view = StaffRegistrationApprovalConfirmView(
        adapter=adapter,
        context=context,
        selected_persona_id=None,
        page=0,
        request=queries.request,
    )
    first = RecordingInteraction(interaction_id=901)
    duplicate = RecordingInteraction(interaction_id=902)

    asyncio.run(
        adapter.approve(
            first,
            context=context,
            request=queries.request,
            source_view=source_view,
        )
    )
    asyncio.run(
        adapter.approve(
            duplicate,
            context=context,
            request=queries.request,
            source_view=source_view,
        )
    )

    assert len(commands.approvals) == 1
    assert authorization.calls == [
        (first, "staff.persona"),
        (duplicate, "staff.persona"),
    ]
    assert first.response.defers == [{"thinking": False}]
    assert duplicate.response.defers == [{"thinking": False}]
    duplicate_terminal = duplicate.edits[-1]["view"]
    assert isinstance(duplicate_terminal, discord.ui.LayoutView)
    assert "이미 처리 중이거나 완료되었습니다" in _layout_text(duplicate_terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in duplicate_terminal.walk_children())


def test_duplicate_rejection_claim_is_terminal_and_never_repeats_mutation() -> None:
    adapter, queries, commands, _, _ = _adapter()
    opener = RecordingInteraction()
    context = StaffPersonaInteractionContext.from_interaction(opener)
    source_view = StaffRegistrationRequestDetailView(
        adapter=adapter,
        context=context,
        selected_persona_id=None,
        page=0,
        request=queries.request,
    )
    first = RecordingInteraction(interaction_id=903)
    duplicate = RecordingInteraction(interaction_id=904)

    for interaction in (first, duplicate):
        asyncio.run(
            adapter.reject(
                interaction,
                context=context,
                request=queries.request,
                reason="검토 완료",
                source_view=source_view,
            )
        )

    assert len(commands.rejections) == 1
    assert first.response.defers == [{"thinking": False}]
    assert duplicate.response.defers == [{"thinking": False}]
    duplicate_terminal = duplicate.edits[-1]["view"]
    assert isinstance(duplicate_terminal, discord.ui.LayoutView)
    assert "이미 처리 중이거나 완료되었습니다" in _layout_text(duplicate_terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in duplicate_terminal.walk_children())
