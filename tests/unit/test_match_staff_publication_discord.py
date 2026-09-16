"""Discord staff Match result publication adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchResultPublicationDiscordAdapter,
    MatchStaffInteractionContext,
    format_match_result_publication_success,
    match_result_publication_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchResultPublicationAlreadyExistsError,
    MatchResultPublicationTargetChoice,
    PublishedMatchResult,
    PublishMatchResult,
    StoredMatchResultPublication,
)
from uma_st2.domain.match import MatchGrade, MatchStatus
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


def _receipt(*, status: PublicationStatus = PublicationStatus.READY) -> PublishedMatchResult:
    return PublishedMatchResult(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        match_status=MatchStatus.SETTLED,
        settlement_fingerprint="a" * 64,
        intent_created_at=NOW,
        publication=StoredMatchResultPublication(
            publication_id=901,
            event_key="match:71:settled-result:v1",
            payload_fingerprint="b" * 64,
            status=status,
            target_channel_id="777777777" if status is PublicationStatus.READY else None,
        ),
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultPublicationTargetChoice, ...]:
        self.calls.append((search, limit))
        return (MatchResultPublicationTargetChoice(71, "제12회 정기전", MatchGrade.G1),)


class RecordingCommands:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[PublishMatchResult] = []

    def publish_result(self, command: PublishMatchResult) -> PublishedMatchResult:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return _receipt()


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingSourceView:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class RecordingInteraction:
    def __init__(self, *, interaction_id: int = 555, fail_edit: bool = False) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=123)
        self.guild_id = 987
        self.channel_id = 654
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_edit:
            raise RuntimeError("delivery failed")
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    *,
    error: Exception | None = None,
) -> tuple[
    MatchResultPublicationDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries()
    commands = RecordingCommands(error=error)
    authorization = RecordingAuthorization()
    return (
        MatchResultPublicationDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            authorize_autocomplete=authorization,
            authorize_interaction=authorization,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        authorization,
    )


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def test_autocomplete_is_authorized_bounded_and_disambiguates_duplicate_names() -> None:
    targets = tuple(
        MatchResultPublicationTargetChoice(index, "@everyone " + "긴이름" * 40, MatchGrade.G1) for index in (71, 72)
    )

    choices = match_result_publication_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert all("ID" in choice.name for choice in choices)


def test_publish_handoff_uses_interaction_key_and_returns_private_committed_receipt() -> None:
    adapter, _, commands, authorization = _adapter()
    interaction = RecordingInteraction(interaction_id=991)
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.publish_result(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.publish")]
    assert source_view.stopped
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-result-publish:991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    payload = interaction.edits[0]
    assert payload["content"] is None
    view = payload["view"]
    assert isinstance(view, discord.ui.LayoutView)
    content = _layout_text(view)
    assert "결과 공개 intent 복구 완료" in content
    assert "`ready`" in content
    assert "@everyone" not in content
    assert not any(isinstance(item, discord.ui.Button) for item in view.walk_children())


def test_existing_logical_publication_returns_bounded_private_rejection() -> None:
    adapter, _, commands, _ = _adapter(error=MatchResultPublicationAlreadyExistsError("already published"))
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.publish_result(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=RecordingSourceView(),  # type: ignore[arg-type]
        )
    )

    assert len(commands.calls) == 1
    assert "이미 존재" in _layout_text(interaction.edits[0]["view"])


def test_delivery_failure_after_source_retirement_sends_fresh_entry_notice() -> None:
    adapter, _, commands, _ = _adapter()
    interaction = RecordingInteraction(fail_edit=True)
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.publish_result(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert source_view.stopped
    assert len(commands.calls) == 1
    assert interaction.followup.messages
    assert "/match staff settlement" in interaction.followup.messages[0][0]


def test_success_formatter_explains_suppressed_and_awaiting_states() -> None:
    suppressed = format_match_result_publication_success(_receipt(status=PublicationStatus.SUPPRESSED))
    awaiting = format_match_result_publication_success(_receipt(status=PublicationStatus.AWAITING_CHANNEL))

    assert "suppressed" in suppressed
    assert "awaiting_channel" in awaiting
    assert "@everyone" not in suppressed + awaiting
