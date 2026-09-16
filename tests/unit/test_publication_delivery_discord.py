"""Discord sender and worker tests for WIN5 publication delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from uma_st2.adapters.discord import (
    DiscordKnownDeliveryFailure,
    DiscordPublicationRenderError,
    DiscordPublicationSenderClient,
    DiscordPublicationSendReceipt,
    DiscordUnknownDeliveryFailure,
    MatchOddsPublicationRuntimeWorker,
    PublicationDeliveryRun,
    PublicationDeliveryWorker,
    PublicationDeliveryWorkerConfig,
    RenderedWin5DiscordPublication,
)
from uma_st2.application.publication import (
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_ROUND_RESULT_EVENT_TYPE,
    ClaimedPublication,
    FinalizePublication,
    PublicationFailureStage,
)
from uma_st2.domain.publication import PublicationStatus


def _claimed(*, publication_id: int = 41, attempt_count: int = 1) -> ClaimedPublication:
    return ClaimedPublication(
        publication_id=publication_id,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
        target_channel_id="123456789",
        payload_json={"publication": publication_id},
        payload_fingerprint="a" * 64,
        attempt_count=attempt_count,
    )


def _config() -> PublicationDeliveryWorkerConfig:
    return PublicationDeliveryWorkerConfig(
        max_batch_size=5,
        max_attempts=3,
        retry_delay=timedelta(minutes=10),
        pending_timeout=timedelta(minutes=20),
    )


class RecordingCommands:
    def __init__(self, claimed: list[ClaimedPublication | None]) -> None:
        self.claimed = claimed
        self.active = False
        self.claim_arguments: list[tuple[timedelta, int]] = []
        self.recovery_arguments: list[tuple[timedelta, int]] = []
        self.finalized: list[FinalizePublication] = []

    def _call(self, operation: Callable[[], object]) -> object:
        assert not self.active
        self.active = True
        try:
            return operation()
        finally:
            self.active = False

    def claim_next(
        self,
        *,
        retry_delay: timedelta,
        max_attempts: int,
    ) -> ClaimedPublication | None:
        def claim() -> ClaimedPublication | None:
            self.claim_arguments.append((retry_delay, max_attempts))
            return self.claimed.pop(0)

        return self._call(claim)  # type: ignore[return-value]

    def recover_stale_pending(
        self,
        *,
        pending_timeout: timedelta,
        max_count: int,
    ) -> int:
        def recover() -> int:
            self.recovery_arguments.append((pending_timeout, max_count))
            return 2

        return self._call(recover)  # type: ignore[return-value]

    def finalize(self, command: FinalizePublication) -> None:
        def finalize() -> None:
            self.finalized.append(command)

        self._call(finalize)


class RecordingSender:
    def __init__(
        self,
        commands: RecordingCommands,
        outcome: DiscordPublicationSendReceipt | Exception,
    ) -> None:
        self.commands = commands
        self.outcome = outcome
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def send(
        self,
        *,
        guild_id: str,
        target_channel_id: str,
        pages: tuple[str, ...],
    ) -> DiscordPublicationSendReceipt:
        assert not self.commands.active
        self.calls.append((guild_id, target_channel_id, pages))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def _direct_application(operation: Callable[[], object]) -> object:
    return operation()


def _renderer(_: str, __: str, ___: object) -> RenderedWin5DiscordPublication:
    return RenderedWin5DiscordPublication(
        publication_type="win5_round_result",
        pages=("page 1", "page 2"),
    )


def test_worker_sends_outside_application_calls_and_finalizes_anchor_once() -> None:
    commands = RecordingCommands([_claimed(), None])
    sender = RecordingSender(commands, DiscordPublicationSendReceipt(anchor_message_id="555555555"))
    worker = PublicationDeliveryWorker(
        commands=commands,  # type: ignore[arg-type]
        sender=sender,
        config=_config(),
        run_application=_direct_application,  # type: ignore[arg-type]
        renderer=_renderer,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result.stale_recovered == 2
    assert result.claimed == 1
    assert result.sent == 1
    assert result.failed == 0
    assert result.delivery_unknown == 0
    assert sender.calls == [("987654321", "123456789", ("page 1", "page 2"))]
    assert commands.finalized == [
        FinalizePublication(
            publication_id=41,
            attempt_count=1,
            status=PublicationStatus.SENT,
            discord_message_id="555555555",
        )
    ]
    assert commands.recovery_arguments == [(timedelta(minutes=20), 5)]
    assert commands.claim_arguments == [
        (timedelta(minutes=10), 3),
        (timedelta(minutes=10), 3),
    ]


class RecordingOddsCommands:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    def refresh_due(self, *, guild_id: str) -> object:
        self.events.append(f"refresh:{guild_id}")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(publication=None)


class RecordingDeliveryWorker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result = PublicationDeliveryRun(0, 0, 0, 0, 0)

    async def run_once(self) -> PublicationDeliveryRun:
        self.events.append("delivery")
        return self.result


@pytest.mark.parametrize("generation_error", (None, RuntimeError("generation failed")))
def test_match_odds_runtime_generation_precedes_delivery_and_cannot_block_it(
    generation_error: Exception | None,
) -> None:
    events: list[str] = []
    odds = RecordingOddsCommands(events, error=generation_error)
    delivery = RecordingDeliveryWorker(events)
    worker = MatchOddsPublicationRuntimeWorker(
        commands=odds,  # type: ignore[arg-type]
        guild_id="987654321",
        delivery_worker=delivery,
        run_application=_direct_application,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert events == ["refresh:987654321", "delivery"]


def test_renderer_rejection_is_known_zero_send_failure() -> None:
    commands = RecordingCommands([_claimed(), None])
    sender = RecordingSender(commands, DiscordPublicationSendReceipt(anchor_message_id="555555555"))

    def reject(_: str, __: str, ___: object) -> RenderedWin5DiscordPublication:
        raise DiscordPublicationRenderError("malformed")

    worker = PublicationDeliveryWorker(
        commands=commands,  # type: ignore[arg-type]
        sender=sender,
        config=_config(),
        run_application=_direct_application,  # type: ignore[arg-type]
        renderer=reject,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result.failed == 1
    assert result.delivery_unknown == 0
    assert sender.calls == []
    assert commands.finalized[0].status == PublicationStatus.FAILED
    assert commands.finalized[0].error_code == "invalid_stored_payload"
    assert commands.finalized[0].failure_stage == PublicationFailureStage.RENDER


def test_partial_provider_failure_is_unknown_and_preserves_anchor() -> None:
    commands = RecordingCommands([_claimed(), None])
    sender = RecordingSender(
        commands,
        DiscordUnknownDeliveryFailure(
            error_code="discord_send_timeout",
            failure_stage=PublicationFailureStage.SEND,
            anchor_message_id="555555555",
        ),
    )
    worker = PublicationDeliveryWorker(
        commands=commands,  # type: ignore[arg-type]
        sender=sender,
        config=_config(),
        run_application=_direct_application,  # type: ignore[arg-type]
        renderer=_renderer,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result.delivery_unknown == 1
    assert commands.finalized[0].status == PublicationStatus.DELIVERY_UNKNOWN
    assert commands.finalized[0].discord_message_id == "555555555"


def test_known_provider_rejection_is_retryable_failed_without_anchor() -> None:
    commands = RecordingCommands([_claimed(), None])
    sender = RecordingSender(
        commands,
        DiscordKnownDeliveryFailure(
            error_code="discord_channel_404_10003",
            failure_stage=PublicationFailureStage.CHANNEL,
        ),
    )
    worker = PublicationDeliveryWorker(
        commands=commands,  # type: ignore[arg-type]
        sender=sender,
        config=_config(),
        run_application=_direct_application,  # type: ignore[arg-type]
        renderer=_renderer,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result.failed == 1
    assert commands.finalized[0].status == PublicationStatus.FAILED
    assert commands.finalized[0].discord_message_id is None


@dataclass(frozen=True)
class FakeGuild:
    id: int


@dataclass(frozen=True)
class FakeMessage:
    id: int


class FakeChannel:
    def __init__(self, *, guild_id: int, outcomes: list[FakeMessage | Exception]) -> None:
        self.guild = FakeGuild(guild_id)
        self.outcomes = outcomes
        self.calls: list[tuple[str, discord.AllowedMentions]] = []

    async def send(
        self,
        *,
        content: str,
        allowed_mentions: discord.AllowedMentions,
    ) -> FakeMessage:
        self.calls.append((content, allowed_mentions))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, cached: object | None) -> None:
        self.cached = cached
        self.fetch_error: Exception | None = None
        self.requested: list[int] = []

    def get_channel(self, channel_id: int) -> object | None:
        self.requested.append(channel_id)
        return self.cached

    async def fetch_channel(self, channel_id: int) -> object:
        self.requested.append(channel_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.cached is None:
            raise RuntimeError("missing fake channel")
        return self.cached


def test_concrete_sender_sends_pages_in_order_and_returns_first_message_anchor() -> None:
    channel = FakeChannel(
        guild_id=987654321,
        outcomes=[FakeMessage(555555555), FakeMessage(666666666)],
    )
    sender = DiscordPublicationSenderClient(FakeClient(channel))

    receipt = asyncio.run(
        sender.send(
            guild_id="987654321",
            target_channel_id="123456789",
            pages=("page 1", "page 2"),
        )
    )

    assert receipt.anchor_message_id == "555555555"
    assert [content for content, _ in channel.calls] == ["page 1", "page 2"]
    assert all(isinstance(mentions, discord.AllowedMentions) for _, mentions in channel.calls)
    assert all(mentions.everyone is False for _, mentions in channel.calls)
    assert all(mentions.users is False for _, mentions in channel.calls)
    assert all(mentions.roles is False for _, mentions in channel.calls)


def test_concrete_sender_marks_failure_after_first_page_as_unknown_with_anchor() -> None:
    channel = FakeChannel(
        guild_id=987654321,
        outcomes=[FakeMessage(555555555), RuntimeError("network outcome unknown")],
    )
    sender = DiscordPublicationSenderClient(FakeClient(channel))

    with pytest.raises(DiscordUnknownDeliveryFailure) as captured:
        asyncio.run(
            sender.send(
                guild_id="987654321",
                target_channel_id="123456789",
                pages=("page 1", "page 2"),
            )
        )

    assert captured.value.anchor_message_id == "555555555"
    assert captured.value.failure_stage == PublicationFailureStage.SEND


def test_channel_resolution_failure_is_known_zero_send() -> None:
    client = FakeClient(None)
    client.fetch_error = RuntimeError("channel unavailable")
    sender = DiscordPublicationSenderClient(client)

    with pytest.raises(DiscordKnownDeliveryFailure) as captured:
        asyncio.run(
            sender.send(
                guild_id="987654321",
                target_channel_id="123456789",
                pages=("page 1",),
            )
        )

    assert captured.value.failure_stage == PublicationFailureStage.CHANNEL
    assert captured.value.error_code == "discord_channel_runtimeerror"


def test_worker_configuration_has_no_implicit_zero_or_unbounded_values() -> None:
    with pytest.raises(ValueError, match="max_batch_size"):
        PublicationDeliveryWorkerConfig(
            max_batch_size=0,
            max_attempts=3,
            retry_delay=timedelta(minutes=1),
            pending_timeout=timedelta(minutes=2),
        )
