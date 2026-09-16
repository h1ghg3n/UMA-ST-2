"""Private Discord settings panel adapter tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from uma_st2.adapters.discord.settings import (
    SettingsChannelKind,
    SettingsChannelView,
    SettingsConfirmView,
    SettingsDiscordAdapter,
    SettingsDraft,
    SettingsInteractionContext,
    SettingsPanelView,
    SettingsReasonModal,
    SettingsTimezoneModal,
    create_settings_command,
)
from uma_st2.application.discord import (
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdatePreview,
    DiscordGuildSettingsUpdateStaleError,
    DiscordGuildSettingsUpdateState,
    UpdatedDiscordGuildSettings,
    UpdateDiscordGuildSettings,
)

NOW = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)


def _editable(**changes: object) -> DiscordGuildEditableSettings:
    values: dict[str, object] = {
        "win5_announcement_channel_id": "201",
        "match_announcement_channel_id": "202",
        "log_channel_id": "203",
        "default_timezone": "Asia/Seoul",
        "win5_announcements_enabled": True,
        "match_announcements_enabled": True,
    }
    values.update(changes)
    return DiscordGuildEditableSettings(**values)  # type: ignore[arg-type]


def _state() -> DiscordGuildSettingsUpdateState:
    return DiscordGuildSettingsUpdateState(
        guild_id="987",
        editable=_editable(),
        operator_role_id="301",
        bot_manager_role_id="302",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


class RecordingQueries:
    def __init__(self, state: DiscordGuildSettingsUpdateState) -> None:
        self.state = state
        self.state_calls: list[str] = []
        self.preview_calls: list[tuple[str, DiscordGuildEditableSettings, str, str]] = []

    def get_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState:
        self.state_calls.append(guild_id)
        return self.state

    def get_preview(
        self,
        *,
        guild_id: str,
        desired: DiscordGuildEditableSettings,
        reason: str,
        expected_state_fingerprint: str,
    ) -> DiscordGuildSettingsUpdatePreview:
        self.preview_calls.append((guild_id, desired, reason, expected_state_fingerprint))
        return DiscordGuildSettingsUpdatePreview(
            before=self.state,
            desired=desired,
            reason=reason,
            changed_fields=self.state.editable.changed_fields(desired),
        )


class RecordingCommands:
    def __init__(self, state: DiscordGuildSettingsUpdateState) -> None:
        self.state = state
        self.calls: list[UpdateDiscordGuildSettings] = []

    def update(self, command: UpdateDiscordGuildSettings) -> UpdatedDiscordGuildSettings:
        self.calls.append(command)
        after = DiscordGuildSettingsUpdateState(
            guild_id=self.state.guild_id,
            editable=command.desired,
            operator_role_id=self.state.operator_role_id,
            bot_manager_role_id=self.state.bot_manager_role_id,
            created_at=self.state.created_at,
            updated_at=NOW + timedelta(seconds=1),
        )
        return UpdatedDiscordGuildSettings(
            operation_id=81,
            before=self.state,
            after=after,
            changed_fields=self.state.editable.changed_fields(command.desired),
            reason=command.reason,
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
        self.response_states: list[bool] = []
        self.allowed = True

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        self.response_states.append(interaction.response.is_done())
        return self.allowed


class RecordingResponse:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []
        self.messages: list[tuple[str | None, dict[str, object]]] = []
        self.defers: list[dict[str, object]] = []
        self.modal: discord.ui.Modal | None = None
        self._done = False

    async def edit_message(self, **kwargs: object) -> None:
        self._done = True
        self.edits.append(kwargs)

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        self._done = True
        self.messages.append((content, kwargs))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self.modal = modal

    async def defer(self, **kwargs: object) -> None:
        self._done = True
        self.defers.append(kwargs)

    def is_done(self) -> bool:
        return self._done


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict[str, object]]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class FakePermissions:
    def __init__(self, *, view: bool, send: bool = False) -> None:
        self.view_channel = view
        self.send_messages = send


class FakeChannel:
    def __init__(self, channel_id: int, *, guild: FakeGuild, permissions: dict[int, FakePermissions]) -> None:
        self.id = channel_id
        self.guild = guild
        self.type = discord.ChannelType.text
        self._permissions = permissions

    def permissions_for(self, target: object) -> FakePermissions:
        return self._permissions[target.id]

    async def send(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeGuild:
    def __init__(self) -> None:
        self.id = 987
        self.owner_id = 999
        self.default_role = SimpleNamespace(id=987)
        self.me = SimpleNamespace(id=401)
        self._roles = {
            301: SimpleNamespace(id=301),
            302: SimpleNamespace(id=302),
        }
        public_permissions = {
            987: FakePermissions(view=True),
            301: FakePermissions(view=True),
            302: FakePermissions(view=True),
            401: FakePermissions(view=True, send=True),
        }
        log_permissions = {
            987: FakePermissions(view=False),
            301: FakePermissions(view=True),
            302: FakePermissions(view=True),
            401: FakePermissions(view=True, send=True),
        }
        private_permissions = {
            987: FakePermissions(view=False),
            301: FakePermissions(view=True),
            302: FakePermissions(view=True),
            401: FakePermissions(view=True, send=True),
        }
        public_log_permissions = {**public_permissions}
        self._channels = {
            201: FakeChannel(201, guild=self, permissions=public_permissions),
            202: FakeChannel(202, guild=self, permissions=public_permissions),
            203: FakeChannel(203, guild=self, permissions=log_permissions),
            204: FakeChannel(204, guild=self, permissions=private_permissions),
            205: FakeChannel(205, guild=self, permissions=public_log_permissions),
        }

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        return self._channels[channel_id]

    def get_role(self, role_id: int) -> object | None:
        return self._roles.get(role_id)


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 900,
        guild: FakeGuild | None = None,
        edit_errors: list[Exception] | None = None,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id, display_name="운영자")
        self.guild_id = 987
        self.channel_id = 654
        self.guild = guild or FakeGuild()
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.edit_attempts = 0
        self.edit_errors = list(edit_errors or ())
        self.observed_view: discord.ui.LayoutView | None = None
        self.observed_view_stopped_at_edit: list[bool] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edit_attempts += 1
        if self.observed_view is not None:
            self.observed_view_stopped_at_edit.append(bool(getattr(self.observed_view, "stop_called", False)))
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.edits.append(kwargs)


class RecordingSettingsPanelView(SettingsPanelView):
    def __init__(
        self,
        *,
        adapter: SettingsDiscordAdapter,
        context: SettingsInteractionContext,
        draft: SettingsDraft,
    ) -> None:
        super().__init__(adapter=adapter, context=context, draft=draft)
        self.stop_called = False

    def stop(self) -> None:
        self.stop_called = True
        super().stop()


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    SettingsDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingPreparation,
    RecordingAuthorization,
]:
    state = _state()
    queries = RecordingQueries(state)
    commands = RecordingCommands(state)
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        SettingsDiscordAdapter(
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


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_direct_root_command_opens_private_detached_panel() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    command = create_settings_command(adapter)
    interaction = RecordingInteraction()

    asyncio.run(command.callback(interaction))

    assert isinstance(command, app_commands.Command)
    assert not isinstance(command, app_commands.Group)
    assert command.name == "settings"
    assert preparation.calls == [(interaction, "settings", True)]
    assert authorization.calls == []
    assert queries.state_calls == ["987"]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, SettingsPanelView)
    assert _button(view, label="WIN5 공지 채널 변경")
    assert _button(view, label="Match 공지 채널 변경")
    assert _button(view, label="로그 채널 변경")
    assert _button(view, label="시간대 설정")
    assert _button(view, label="WIN5 공지 해제").style is discord.ButtonStyle.danger
    assert _button(view, label="Match 공지 해제").style is discord.ButtonStyle.danger
    assert _button(view, label="변경 검토").style is discord.ButtonStyle.success


def test_channel_editor_acknowledges_before_database_authorization() -> None:
    adapter, _queries, _commands, _preparation, authorization = _adapter()
    opener = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(opener)
    panel = SettingsPanelView(
        adapter=adapter,
        context=context,
        draft=SettingsDraft.from_state(_state()),
    )
    interaction = RecordingInteraction()

    asyncio.run(_button(panel, label="WIN5 공지 채널 변경").callback(interaction))

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.response_states == [True]
    assert isinstance(interaction.edits[0]["view"], SettingsChannelView)


def test_timezone_toggle_and_clear_channel_only_replace_local_draft() -> None:
    adapter, _queries, commands, _preparation, authorization = _adapter()
    interaction = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state())

    asyncio.run(adapter.open_timezone_modal(interaction, context=context, draft=draft))
    assert isinstance(interaction.response.modal, SettingsTimezoneModal)
    assert interaction.response.modal.title == "시간대 설정 변경"
    assert str(interaction.response.modal.timezone.default) == "Asia/Seoul"

    timezone_interaction = RecordingInteraction()
    asyncio.run(
        adapter.set_timezone(
            timezone_interaction,
            context=context,
            draft=draft,
            timezone="UTC",
        )
    )
    assert timezone_interaction.response.defers == [{"thinking": False}]
    assert isinstance(timezone_interaction.edits[0]["view"], SettingsPanelView)

    toggle_interaction = RecordingInteraction()
    toggle_panel = RecordingSettingsPanelView(adapter=adapter, context=context, draft=draft)
    toggle_interaction.observed_view = toggle_panel
    asyncio.run(
        adapter.toggle_announcements(
            toggle_interaction,
            context=context,
            draft=draft,
            win5=True,
            source_view=toggle_panel,
        )
    )
    assert toggle_interaction.response.defers == [{"thinking": False}]
    assert toggle_interaction.observed_view_stopped_at_edit == [True]
    toggled_view = toggle_interaction.edits[0]["view"]
    assert isinstance(toggled_view, SettingsPanelView)
    assert _button(toggled_view, label="WIN5 공지 사용").style is discord.ButtonStyle.success
    assert _button(toggled_view, label="Match 공지 해제").style is discord.ButtonStyle.danger

    clear_interaction = RecordingInteraction()
    asyncio.run(
        adapter.clear_channel(
            clear_interaction,
            context=context,
            draft=draft,
            kind=SettingsChannelKind.MATCH,
            source_view=None,
        )
    )

    assert clear_interaction.response.defers == [{"thinking": False}]
    assert isinstance(clear_interaction.edits[0]["view"], SettingsPanelView)
    assert commands.calls == []
    assert [name for _, name in authorization.calls] == [
        "settings",
        "settings",
        "settings",
        "settings",
    ]
    assert authorization.response_states == [False, True, True, True]


def test_channel_selection_enforces_public_announcement_and_private_staff_log() -> None:
    adapter, _queries, commands, _preparation, _authorization = _adapter()
    draft = SettingsDraft.from_state(_state())
    context = SettingsInteractionContext.from_interaction(RecordingInteraction())

    private_announcement = RecordingInteraction()
    asyncio.run(
        adapter.set_channel(
            private_announcement,
            context=context,
            draft=draft,
            kind=SettingsChannelKind.WIN5,
            channel_id=204,
            source_view=None,
        )
    )
    assert private_announcement.response.defers == [{"thinking": False}]
    assert private_announcement.followup.messages[0][0] is not None
    assert "권한 조건" in str(private_announcement.followup.messages[0][0])
    assert isinstance(private_announcement.edits[0]["view"], SettingsPanelView)

    public_log = RecordingInteraction()
    asyncio.run(
        adapter.set_channel(
            public_log,
            context=context,
            draft=draft,
            kind=SettingsChannelKind.LOG,
            channel_id=205,
            source_view=None,
        )
    )
    assert public_log.response.defers == [{"thinking": False}]
    assert "권한 조건" in str(public_log.followup.messages[0][0])

    valid_public = RecordingInteraction()
    asyncio.run(
        adapter.set_channel(
            valid_public,
            context=context,
            draft=draft,
            kind=SettingsChannelKind.MATCH,
            channel_id=201,
            source_view=None,
        )
    )
    valid_log = RecordingInteraction()
    asyncio.run(
        adapter.set_channel(
            valid_log,
            context=context,
            draft=draft,
            kind=SettingsChannelKind.LOG,
            channel_id=203,
            source_view=None,
        )
    )

    assert isinstance(valid_public.edits[0]["view"], SettingsPanelView)
    assert isinstance(valid_log.edits[0]["view"], SettingsPanelView)
    assert commands.calls == []


def test_review_is_zero_write_and_final_confirm_uses_final_interaction_key() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    initial = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(initial)
    draft = SettingsDraft.from_state(_state()).with_timezone("UTC")

    modal_interaction = RecordingInteraction(interaction_id=556)
    asyncio.run(adapter.open_reason_modal(modal_interaction, context=context, draft=draft))
    assert isinstance(modal_interaction.response.modal, SettingsReasonModal)
    assert modal_interaction.response.modal.reason.required is True

    preview_interaction = RecordingInteraction(interaction_id=557)
    asyncio.run(
        adapter.show_preview(
            preview_interaction,
            context=context,
            draft=draft,
            reason="운영 timezone 정정",
        )
    )
    assert preview_interaction.response.defers == [{"thinking": False}]
    view = preview_interaction.edits[0]["view"]
    assert isinstance(view, SettingsConfirmView)
    assert _button(view, label="다시 수정")
    assert len(queries.preview_calls) == 1
    assert commands.calls == []

    final_interaction = RecordingInteraction(interaction_id=777)
    asyncio.run(_button(view, label="변경 확정").callback(final_interaction))

    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.actor_discord_user_id == "900"
    assert command.desired.default_timezone == "UTC"
    assert preparation.calls == []
    assert final_interaction.response.defers == [{"thinking": False}]
    assert [name for _, name in authorization.calls] == ["settings", "settings", "settings"]
    assert authorization.response_states == [False, True, True]
    final_edit = final_interaction.edits[0]
    assert final_edit["content"] is None
    final_view = final_edit["view"]
    assert isinstance(final_view, discord.ui.LayoutView)
    assert "설정 변경 완료" in _layout_text(final_view)
    assert not any(isinstance(item, discord.ui.Button) for item in final_view.walk_children())


def test_close_replaces_the_components_v2_message_with_a_terminal_layout() -> None:
    adapter, _queries, commands, _preparation, _authorization = _adapter()
    interaction = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(interaction)
    source_view = RecordingSettingsPanelView(
        adapter=adapter,
        context=context,
        draft=SettingsDraft.from_state(_state()),
    )

    asyncio.run(adapter.close(interaction, context=context, source_view=source_view))

    assert commands.calls == []
    assert interaction.response.defers == [{"thinking": False}]
    final_edit = interaction.edits[0]
    assert final_edit["content"] is None
    final_view = final_edit["view"]
    assert isinstance(final_view, discord.ui.LayoutView)
    assert _layout_text(final_view) == "설정 화면을 닫았습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in final_view.walk_children())
    assert source_view.stop_called is True


def test_confirm_does_not_reclassify_a_committed_update_when_receipt_delivery_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _queries, commands, _preparation, _authorization = _adapter()
    interaction = RecordingInteraction(edit_errors=[RuntimeError("receipt delivery failed")])
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state()).with_timezone("UTC")
    preview = DiscordGuildSettingsUpdatePreview(
        before=draft.original,
        desired=draft.desired,
        reason="운영 timezone 정정",
        changed_fields=draft.original.editable.changed_fields(draft.desired),
    )
    source_view = RecordingSettingsPanelView(
        adapter=adapter,
        context=context,
        draft=draft,
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            adapter.confirm(
                interaction,
                context=context,
                preview=preview,
                source_view=source_view,
            )
        )

    assert len(commands.calls) == 1
    assert interaction.edit_attempts == 1
    assert interaction.edits == []
    assert len(interaction.followup.messages) == 1
    assert "설정 변경 완료" in str(interaction.followup.messages[0][0])
    assert "operation=confirm-receipt" in caplog.text
    assert "operation=confirm actor_id" not in caplog.text
    assert source_view.stop_called is True


def test_view_replacement_failure_stops_the_old_view_and_requires_fresh_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _queries, commands, _preparation, _authorization = _adapter()
    interaction = RecordingInteraction(edit_errors=[RuntimeError("replacement failed")])
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state())
    source_view = RecordingSettingsPanelView(adapter=adapter, context=context, draft=draft)

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            adapter.toggle_announcements(
                interaction,
                context=context,
                draft=draft,
                win5=True,
                source_view=source_view,
            )
        )

    assert commands.calls == []
    assert interaction.edit_attempts == 1
    assert interaction.edits == []
    assert source_view.stop_called is True
    assert "operation=replace-view" in caplog.text
    assert len(interaction.followup.messages) == 1
    assert "/settings" in str(interaction.followup.messages[0][0])


def test_modal_submit_from_a_replaced_source_view_is_rejected_before_defer_or_write() -> None:
    adapter, _queries, commands, _preparation, authorization = _adapter()
    interaction = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state())
    source_view = RecordingSettingsPanelView(adapter=adapter, context=context, draft=draft)
    source_view.is_finished = lambda: True  # type: ignore[method-assign]

    asyncio.run(
        adapter.set_timezone(
            interaction,
            context=context,
            draft=draft,
            timezone="UTC",
            source_view=source_view,
        )
    )

    assert commands.calls == []
    assert authorization.calls == []
    assert interaction.response.defers == []
    assert interaction.edits == []
    assert len(interaction.response.messages) == 1
    assert "종료되거나 교체된 설정 화면" in str(interaction.response.messages[0][0])


def test_stale_review_closes_the_invalid_draft_view() -> None:
    adapter, queries, commands, _preparation, _authorization = _adapter()
    interaction = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state()).with_timezone("UTC")
    source_view = RecordingSettingsPanelView(adapter=adapter, context=context, draft=draft)

    def stale_preview(**_kwargs: object) -> DiscordGuildSettingsUpdatePreview:
        raise DiscordGuildSettingsUpdateStaleError("stale")

    queries.get_preview = stale_preview  # type: ignore[method-assign]
    asyncio.run(
        adapter.show_preview(
            interaction,
            context=context,
            draft=draft,
            reason="stale 초안 종료",
            source_view=source_view,
        )
    )

    assert commands.calls == []
    assert interaction.response.defers == [{"thinking": False}]
    assert len(interaction.edits) == 1
    final_edit = interaction.edits[0]
    assert final_edit["content"] is None
    final_view = final_edit["view"]
    assert isinstance(final_view, discord.ui.LayoutView)
    assert _layout_text(final_view) == "설정이 다른 작업에서 변경되었습니다. `/settings`를 다시 열어 주세요."
    assert not any(isinstance(item, discord.ui.Button) for item in final_view.walk_children())
    allowed_mentions = final_edit["allowed_mentions"]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert source_view.stop_called is True


def test_revoked_current_authority_closes_the_component_view() -> None:
    adapter, _queries, commands, _preparation, authorization = _adapter()
    authorization.allowed = False
    interaction = RecordingInteraction()
    context = SettingsInteractionContext.from_interaction(interaction)
    draft = SettingsDraft.from_state(_state())
    source_view = RecordingSettingsPanelView(adapter=adapter, context=context, draft=draft)

    asyncio.run(
        adapter.toggle_announcements(
            interaction,
            context=context,
            draft=draft,
            win5=True,
            source_view=source_view,
        )
    )

    assert commands.calls == []
    assert interaction.response.defers == [{"thinking": False}]
    assert len(interaction.edits) == 1
    final_edit = interaction.edits[0]
    assert final_edit["content"] is None
    final_view = final_edit["view"]
    assert isinstance(final_view, discord.ui.LayoutView)
    assert _layout_text(final_view) == "현재 설정 권한을 확인할 수 없어 화면을 종료했습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in final_view.walk_children())
    assert source_view.stop_called is True


def test_foreign_component_is_rejected_before_query_or_write() -> None:
    adapter, queries, commands, preparation, authorization = _adapter()
    opener = RecordingInteraction()
    foreign = RecordingInteraction(user_id=901)
    context = SettingsInteractionContext.from_interaction(opener)
    draft = SettingsDraft.from_state(_state()).with_timezone("UTC")

    asyncio.run(
        adapter.show_preview(
            foreign,
            context=context,
            draft=draft,
            reason="권한 경계 확인",
        )
    )

    assert queries.preview_calls == []
    assert commands.calls == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert "화면을 연 운영자" in str(foreign.response.messages[0][0])
