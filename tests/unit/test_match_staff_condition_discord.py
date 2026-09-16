"""Discord native Match condition adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import get_ident
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchConditionConfirmView,
    MatchConditionDiscordAdapter,
    MatchConditionPreview,
    MatchStaffInteractionContext,
    format_match_condition_preview,
    match_condition_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchConditionAuditType,
    MatchConditionRecord,
    MatchConditionTarget,
    MatchConditionValues,
    SetMatchConditions,
    UpdatedMatchConditions,
)
from uma_st2.domain.match import (
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)

NOW = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _values(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=weather,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _target(
    *,
    match_id: int = 71,
    current: bool = False,
    name: str = "제12회 @everyone 정기전",
) -> MatchConditionTarget:
    condition = None
    version = None
    if current:
        condition = MatchConditionRecord(values=_values(), created_at=NOW, updated_at=NOW)
        version = 31
    return MatchConditionTarget(
        match_id=match_id,
        name=name,
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        scheduled_at=SCHEDULED_AT,
        condition=condition,
        condition_version=version,
    )


class RecordingQueries:
    def __init__(self, target: MatchConditionTarget | None = None) -> None:
        self.target = _target() if target is None else target
        self.search_calls: list[tuple[str, int, int]] = []
        self.get_calls: list[tuple[int, int]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchConditionTarget, ...]:
        self.search_calls.append((search, limit, get_ident()))
        return (self.target,)

    def get_target(self, *, match_id: int) -> MatchConditionTarget | None:
        self.get_calls.append((match_id, get_ident()))
        return self.target if self.target.match_id == match_id else None


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[SetMatchConditions, int]] = []

    def set_conditions(self, command: SetMatchConditions) -> UpdatedMatchConditions:
        self.calls.append((command, get_ident()))
        created_at = NOW if command.expected_condition is None else command.expected_condition.created_at
        return UpdatedMatchConditions(
            snapshot=MatchConditionTarget(
                match_id=command.match_id,
                name="제12회 @everyone 정기전",
                source_kind=MatchSourceKind.NATIVE_V2,
                status=MatchStatus.SCHEDULED,
                scheduled_at=SCHEDULED_AT,
                condition=MatchConditionRecord(
                    values=command.values,
                    created_at=created_at,
                    updated_at=NOW,
                ),
                condition_version=command.expected_condition_version,
            ),
            operation_type=(
                MatchConditionAuditType.SET if command.expected_condition is None else MatchConditionAuditType.CHANGED
            ),
        )


class RecordingPreparation:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return self.allowed


class RecordingAutocompleteAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


class RecordingAuthorization(RecordingAutocompleteAuthorization):
    pass


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    edits: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _worker[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return await asyncio.get_running_loop().run_in_executor(executor, operation)


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _layout_button(view: discord.ui.LayoutView, *, custom_id: str) -> discord.ui.Button:
    return next(
        item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.custom_id == custom_id
    )


def _adapter(
    *,
    target: MatchConditionTarget | None = None,
    preparation: RecordingPreparation | None = None,
    autocomplete_authorization: RecordingAutocompleteAuthorization | None = None,
    authorization: RecordingAuthorization | None = None,
) -> tuple[MatchConditionDiscordAdapter, RecordingQueries, RecordingCommands]:
    queries = RecordingQueries(target)
    commands = RecordingCommands()
    return (
        MatchConditionDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            prepare_command=preparation or RecordingPreparation(),
            authorize_autocomplete=autocomplete_authorization or RecordingAutocompleteAuthorization(),
            authorize_interaction=authorization or RecordingAuthorization(),
            blocking_runner=_worker,
        ),
        queries,
        commands,
    )


def test_initial_preview_is_zero_write_and_final_confirm_uses_component_key() -> None:
    adapter, queries, commands = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.preview_conditions(
            interaction,  # type: ignore[arg-type]
            match_id=71,
            season="autumn",
            weather="sunny",
            time_of_day="day",
            track_condition="firm",
            reason=None,
        )
    )

    assert commands.calls == []
    assert queries.get_calls and queries.get_calls[0][1] != get_ident()
    view = interaction.edits[-1]["view"]
    assert isinstance(view, MatchConditionConfirmView)
    assert interaction.edits[-1]["content"] is None
    assert "제12회" in _layout_text(view)

    confirm = RecordingInteraction(interaction_id=777)
    asyncio.run(_layout_button(view, custom_id="match-condition-confirm").callback(confirm))  # type: ignore[arg-type]

    assert len(commands.calls) == 1
    command, worker_thread = commands.calls[0]
    assert command.idempotency_key == "match-condition:777"
    assert command.expected_condition is None
    assert worker_thread != get_ident()
    terminal = confirm.edits[-1]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert confirm.edits[-1]["content"] is None
    assert "저장 완료" in _layout_text(terminal)
    assert "@everyone" not in _layout_text(terminal)
    assert view.is_finished()


def test_existing_condition_change_requires_reason_before_preview() -> None:
    adapter, _queries, commands = _adapter(target=_target(current=True))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.preview_conditions(
            interaction,  # type: ignore[arg-type]
            match_id=71,
            season="autumn",
            weather="rain",
            time_of_day="day",
            track_condition="firm",
            reason="",
        )
    )

    assert commands.calls == []
    terminal = interaction.edits[-1]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert interaction.edits[-1]["content"] is None
    assert "사유" in _layout_text(terminal)


def test_cancel_closes_source_with_buttonless_terminal_layout() -> None:
    adapter, _queries, commands = _adapter()
    preview = MatchConditionPreview(target=_target(), values=_values(), reason=None)
    view = MatchConditionConfirmView(
        adapter=adapter,
        context=MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654),
        preview=preview,
    )
    interaction = RecordingInteraction()

    asyncio.run(_layout_button(view, custom_id="match-condition-cancel").callback(interaction))  # type: ignore[arg-type]

    assert commands.calls == []
    terminal = interaction.response.edits[-1]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert interaction.response.edits[-1]["content"] is None
    assert "취소" in _layout_text(terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())


def test_duplicate_confirmation_invokes_command_once_and_closes_layout() -> None:
    adapter, _queries, commands = _adapter()
    preview = MatchConditionPreview(target=_target(), values=_values(), reason=None)
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    view = MatchConditionConfirmView(adapter=adapter, context=context, preview=preview)

    asyncio.run(
        adapter.confirm_conditions(
            RecordingInteraction(interaction_id=777),  # type: ignore[arg-type]
            context=context,
            preview=preview,
            source_view=view,
        )
    )
    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(
        adapter.confirm_conditions(
            duplicate,  # type: ignore[arg-type]
            context=context,
            preview=preview,
            source_view=view,
        )
    )

    assert len(commands.calls) == 1
    terminal = duplicate.edits[-1]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Preview" in _layout_text(terminal)


def test_bound_context_mismatch_rejects_before_final_command() -> None:
    adapter, _queries, commands = _adapter()
    preview = MatchConditionPreview(target=_target(), values=_values(), reason=None)
    view = MatchConditionConfirmView(
        adapter=adapter,
        context=MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654),
        preview=preview,
    )
    mismatch = RecordingInteraction(user_id=999)

    asyncio.run(
        adapter.confirm_conditions(
            mismatch,  # type: ignore[arg-type]
            context=MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654),
            preview=preview,
            source_view=view,
        )
    )

    assert commands.calls == []
    assert mismatch.response.messages
    assert not view.is_finished()


def test_autocomplete_revalidates_authorization_and_returns_safe_bounded_choice() -> None:
    authorization = RecordingAutocompleteAuthorization()
    adapter, queries, _commands = _adapter(autocomplete_authorization=authorization)
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_targets(interaction, "정기"))  # type: ignore[arg-type]

    assert authorization.calls[0][1] == "match.staff.race-condition-set"
    assert queries.search_calls and queries.search_calls[0][:2] == ("정기", 25)
    assert choices[0].value == 71
    assert "@everyone" not in choices[0].name
    assert len(choices[0].name) <= 100


def test_preview_and_duplicate_autocomplete_labels_are_bounded_and_safe() -> None:
    preview = MatchConditionPreview(target=_target(), values=_values(), reason=None)
    rendered = format_match_condition_preview(preview)
    choices = match_condition_autocomplete_choices((_target(name="동일"), _target(match_id=72, name="동일")))

    assert "@everyone" not in rendered
    assert len(rendered) <= 1900
    assert all("ID" in choice.name for choice in choices)
