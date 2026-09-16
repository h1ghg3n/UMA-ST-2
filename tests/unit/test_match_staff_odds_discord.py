"""Private Discord controls for periodic Match odds publication mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchOddsModeDiscordAdapter,
    MatchOddsModeView,
    MatchStaffInteractionContext,
)
from uma_st2.application.match import (
    ChangedMatchOddsRefreshMode,
    ChangeMatchOddsRefreshMode,
    MatchOddsPublicationNoChangeError,
    MatchOddsPublicationUnavailableError,
    MatchOddsRefreshStatus,
    StoredMatchOddsRefreshPublication,
)
from uma_st2.application.publication import MatchOddsRefreshMode
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)


@dataclass
class RecordingResponse:
    edits: list[dict[str, object]] = field(default_factory=list)
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)
    done: bool = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self.done = True


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingInteraction:
    def __init__(
        self,
        *,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
        fail_edit: bool = False,
    ) -> None:
        self.id = 555
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_edit:
            raise RuntimeError("simulated edit failure")
        self.edits.append(kwargs)


class RecordingQueries:
    def __init__(self, status: MatchOddsRefreshStatus, *, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.guild_ids: list[str] = []

    def get_status(self, *, guild_id: str) -> MatchOddsRefreshStatus:
        self.guild_ids.append(guild_id)
        if self.error is not None:
            raise self.error
        return self.status


class RecordingCommands:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[ChangeMatchOddsRefreshMode] = []

    def change_mode(self, command: ChangeMatchOddsRefreshMode) -> ChangedMatchOddsRefreshMode:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return ChangedMatchOddsRefreshMode(
            guild_id=command.guild_id,
            previous_mode=MatchOddsRefreshMode.NORMAL,
            mode=MatchOddsRefreshMode.LIVE,
            open_match_count=2,
            next_refresh_at=NOW + timedelta(minutes=1),
            changed_at=NOW,
            publication=StoredMatchOddsRefreshPublication(
                publication_id=91,
                event_key="match-odds-refresh:3:v1",
                payload_fingerprint="a" * 64,
                status=PublicationStatus.READY,
                target_channel_id="777777777",
            ),
        )


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


class RecordingNavigation:
    def __init__(self) -> None:
        self.calls: list[tuple[object, MatchStaffInteractionContext, object]] = []

    async def return_to_race_panel(
        self,
        interaction: object,
        *,
        context: MatchStaffInteractionContext,
        source_view: object,
    ) -> None:
        self.calls.append((interaction, context, source_view))


async def _direct_application(operation: object) -> object:
    return operation()  # type: ignore[operator]


def _status() -> MatchOddsRefreshStatus:
    return MatchOddsRefreshStatus(
        guild_id="987",
        mode=MatchOddsRefreshMode.NORMAL,
        next_refresh_at=NOW + timedelta(minutes=10),
        open_match_count=2,
    )


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _track_stop(view: discord.ui.LayoutView) -> list[bool]:
    calls: list[bool] = []
    original_stop = view.stop

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def test_status_view_is_private_bound_and_marks_current_mode_disabled() -> None:
    queries = RecordingQueries(_status())
    adapter = MatchOddsModeDiscordAdapter(
        queries=queries,  # type: ignore[arg-type]
        commands=RecordingCommands(),  # type: ignore[arg-type]
        authorize_interaction=RecordingAuthorization(),
        run_application=_direct_application,  # type: ignore[arg-type]
    )
    navigation = RecordingNavigation()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = discord.ui.LayoutView(timeout=600)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.show_status(
            interaction,  # type: ignore[arg-type]
            navigation=navigation,
            context=context,
            source_view=source,
        )
    )

    assert queries.guild_ids == ["987"]
    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchOddsModeView)
    assert _button(view, "NORMAL로 전환").disabled is True
    assert _button(view, "LIVE로 전환").disabled is False

    mismatch = RecordingInteraction(user_id=999)
    asyncio.run(
        adapter.show_status(
            mismatch,  # type: ignore[arg-type]
            navigation=navigation,
            context=context,
            source_view=None,
        )
    )
    assert mismatch.response.messages[0][1]["ephemeral"] is True
    assert mismatch.response.defers == []
    assert queries.guild_ids == ["987"]


def test_status_query_error_keeps_source_view_live_and_uses_deferred_followup() -> None:
    queries = RecordingQueries(_status(), error=MatchOddsPublicationUnavailableError("closed"))
    adapter = MatchOddsModeDiscordAdapter(
        queries=queries,  # type: ignore[arg-type]
        commands=RecordingCommands(),  # type: ignore[arg-type]
        authorize_interaction=RecordingAuthorization(),
        run_application=_direct_application,  # type: ignore[arg-type]
    )
    source = discord.ui.LayoutView(timeout=600)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.show_status(
            interaction,  # type: ignore[arg-type]
            navigation=RecordingNavigation(),
            context=MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654),
            source_view=source,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages[0][1]["ephemeral"] is True


def test_live_button_reauthorizes_and_uses_interaction_id_for_one_final_command() -> None:
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    adapter = MatchOddsModeDiscordAdapter(
        queries=RecordingQueries(_status()),  # type: ignore[arg-type]
        commands=commands,  # type: ignore[arg-type]
        authorize_interaction=authorization,
        run_application=_direct_application,  # type: ignore[arg-type]
    )
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    navigation = RecordingNavigation()
    view = MatchOddsModeView(
        adapter=adapter,
        navigation=navigation,
        context=context,
        status=_status(),
    )
    stop_calls = _track_stop(view)
    interaction = RecordingInteraction()

    asyncio.run(_button(view, "LIVE로 전환").callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "match.staff.race")]
    assert interaction.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.guild_id == "987"
    assert command.actor_discord_user_id == "123"
    assert command.desired_mode is MatchOddsRefreshMode.LIVE
    assert command.idempotency_key == "match-odds-mode:555"
    assert stop_calls == [True]
    updated_view = interaction.edits[0]["view"]
    assert isinstance(updated_view, MatchOddsModeView)
    assert _button(updated_view, "LIVE로 전환").disabled is True


def test_no_change_error_is_private_and_does_not_replace_the_bound_view() -> None:
    commands = RecordingCommands(error=MatchOddsPublicationNoChangeError("same"))
    adapter = MatchOddsModeDiscordAdapter(
        queries=RecordingQueries(_status()),  # type: ignore[arg-type]
        commands=commands,  # type: ignore[arg-type]
        authorize_interaction=RecordingAuthorization(),
        run_application=_direct_application,  # type: ignore[arg-type]
    )
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchOddsModeView(
        adapter=adapter,
        navigation=RecordingNavigation(),
        context=context,
        status=_status(),
    )
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.change_mode(
            interaction,  # type: ignore[arg-type]
            navigation=RecordingNavigation(),
            context=context,
            desired_mode=MatchOddsRefreshMode.NORMAL,
            source_view=source,
        )
    )

    assert len(commands.calls) == 1
    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages[0][1]["ephemeral"] is True


def test_successful_mode_change_edit_failure_closes_source_and_sends_reopen_notice() -> None:
    commands = RecordingCommands()
    adapter = MatchOddsModeDiscordAdapter(
        queries=RecordingQueries(_status()),  # type: ignore[arg-type]
        commands=commands,  # type: ignore[arg-type]
        authorize_interaction=RecordingAuthorization(),
        run_application=_direct_application,  # type: ignore[arg-type]
    )
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchOddsModeView(
        adapter=adapter,
        navigation=RecordingNavigation(),
        context=context,
        status=_status(),
    )
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction(fail_edit=True)

    asyncio.run(
        adapter.change_mode(
            interaction,  # type: ignore[arg-type]
            navigation=RecordingNavigation(),
            context=context,
            desired_mode=MatchOddsRefreshMode.LIVE,
            source_view=source,
        )
    )

    assert len(commands.calls) == 1
    assert stop_calls == [True]
    assert interaction.followup.messages
    assert "/match staff race" in interaction.followup.messages[0][0]
