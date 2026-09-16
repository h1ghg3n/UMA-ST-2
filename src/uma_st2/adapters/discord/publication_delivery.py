"""Discord sender and run-once worker for supported stored publications."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import discord

from uma_st2.application.match import MatchOddsPublicationCommands
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETS_REFUNDED_EVENT_TYPE,
    MATCH_BETTING_CLOSED_EVENT_TYPE,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    MATCH_ODDS_REFRESH_EVENT_TYPE,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_HALL_OF_FAME_EVENT_TYPE,
    WIN5_ROUND_RESULT_EVENT_TYPE,
    ClaimedPublication,
    FinalizePublication,
    PublicationDeliveryCommands,
    PublicationFailureStage,
)
from uma_st2.domain.publication import PublicationStatus

from .common import BlockingApplicationRunner, run_blocking_application
from .match_publication import (
    MatchDiscordPublicationRenderError,
    render_match_discord_publication,
)
from .win5_publication import (
    Win5DiscordPublicationRenderError,
    render_win5_discord_publication,
)

logger = logging.getLogger(__name__)


class DiscordPublicationRenderError(ValueError):
    """A supported stored publication cannot be rendered safely."""


class RenderedDiscordPublication(Protocol):
    """Minimal provider-independent result consumed by the sender."""

    @property
    def pages(self) -> tuple[str, ...]: ...


def render_discord_publication(
    destination_kind: str,
    event_type: str,
    payload_json: Mapping[str, object],
) -> RenderedDiscordPublication:
    """Dispatch one supported stored route without reading current state."""

    if destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND and event_type in {
        WIN5_ROUND_RESULT_EVENT_TYPE,
        WIN5_HALL_OF_FAME_EVENT_TYPE,
    }:
        try:
            return render_win5_discord_publication(payload_json)
        except Win5DiscordPublicationRenderError as exc:
            raise DiscordPublicationRenderError("Stored WIN5 publication payload is malformed.") from exc
    match_publication_types = {
        MATCH_BETTING_OPENED_EVENT_TYPE: "match_betting_opening",
        MATCH_ODDS_REFRESH_EVENT_TYPE: "match_odds_refresh",
        MATCH_BETTING_CLOSED_EVENT_TYPE: "match_betting_closed",
        MATCH_BETS_REFUNDED_EVENT_TYPE: "match_bet_refund_completed",
        MATCH_RESULT_CONFIRMED_EVENT_TYPE: "match_settled_result",
        MATCH_SETTLEMENT_VOIDED_EVENT_TYPE: "match_settlement_voided",
    }
    expected_match_publication_type = match_publication_types.get(event_type)
    if destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND and expected_match_publication_type is not None:
        try:
            rendered = render_match_discord_publication(payload_json)
        except MatchDiscordPublicationRenderError as exc:
            raise DiscordPublicationRenderError("Stored Match publication payload is malformed.") from exc
        if rendered.publication_type != expected_match_publication_type:
            raise DiscordPublicationRenderError("Stored Match publication route and payload disagree.")
        return rendered
    raise DiscordPublicationRenderError("Stored publication route is unsupported.")


class DiscordChannelClient(Protocol):
    """Public discord.py channel lookup surface used by the sender."""

    def get_channel(self, channel_id: int) -> object | None: ...

    async def fetch_channel(self, channel_id: int) -> object: ...


@dataclass(frozen=True, slots=True)
class DiscordPublicationSendReceipt:
    """Provider acknowledgement for a complete logical publication."""

    anchor_message_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.anchor_message_id, str)
            or not self.anchor_message_id.isascii()
            or not self.anchor_message_id.isdecimal()
            or int(self.anchor_message_id) <= 0
            or len(self.anchor_message_id) > 32
        ):
            raise ValueError("anchor_message_id must be a positive decimal Discord snowflake.")


class DiscordPublicationSender(Protocol):
    """Send every rendered page for one claimed logical publication."""

    async def send(
        self,
        *,
        guild_id: str,
        target_channel_id: str,
        pages: tuple[str, ...],
    ) -> DiscordPublicationSendReceipt: ...


class DiscordKnownDeliveryFailure(Exception):
    """Provider create is known not to have acknowledged any message."""

    def __init__(
        self,
        *,
        error_code: str,
        failure_stage: PublicationFailureStage,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.failure_stage = failure_stage


class DiscordUnknownDeliveryFailure(Exception):
    """Provider outcome or a partial multi-page delivery is ambiguous."""

    def __init__(
        self,
        *,
        error_code: str,
        failure_stage: PublicationFailureStage,
        anchor_message_id: str | None = None,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.failure_stage = failure_stage
        self.anchor_message_id = anchor_message_id


def _stable_discord_error_code(stage: str, error: Exception) -> str:
    parts = ["discord", stage]
    status = getattr(error, "status", None)
    provider_code = getattr(error, "code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(str(status))
    if isinstance(provider_code, int) and not isinstance(provider_code, bool):
        parts.append(str(provider_code))
    if len(parts) == 2:
        type_name = "".join(character if character.isalnum() else "_" for character in type(error).__name__.lower())
        parts.append(type_name or "error")
    return "_".join(parts)[:64]


def _positive_snowflake(value: str, *, field_name: str) -> int:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0 or len(value) > 32:
        raise ValueError(f"{field_name} must be a positive decimal Discord snowflake.")
    return int(value)


def _known_zero_send_error(error: Exception) -> bool:
    if isinstance(error, (discord.Forbidden, discord.NotFound)):
        return True
    return isinstance(error, discord.HTTPException) and 400 <= error.status < 500


@dataclass(frozen=True, slots=True)
class DiscordPublicationSenderClient:
    """Concrete discord.py 2.7 sender for sequential publication pages."""

    client: DiscordChannelClient

    async def send(
        self,
        *,
        guild_id: str,
        target_channel_id: str,
        pages: tuple[str, ...],
    ) -> DiscordPublicationSendReceipt:
        if not pages:
            raise ValueError("A Discord publication requires at least one page.")
        guild_snowflake = _positive_snowflake(guild_id, field_name="guild_id")
        channel_snowflake = _positive_snowflake(target_channel_id, field_name="target_channel_id")
        channel = await self._resolve_channel(channel_snowflake)
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild_id != guild_snowflake:
            raise DiscordKnownDeliveryFailure(
                error_code="discord_channel_guild_mismatch",
                failure_stage=PublicationFailureStage.CHANNEL,
            )
        send = getattr(channel, "send", None)
        if not callable(send):
            raise DiscordKnownDeliveryFailure(
                error_code="discord_channel_not_messageable",
                failure_stage=PublicationFailureStage.CHANNEL,
            )

        anchor_message_id: str | None = None
        for page in pages:
            try:
                message = await send(
                    content=page,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as error:
                error_code = _stable_discord_error_code("send", error)
                if anchor_message_id is None and _known_zero_send_error(error):
                    raise DiscordKnownDeliveryFailure(
                        error_code=error_code,
                        failure_stage=PublicationFailureStage.SEND,
                    ) from error
                raise DiscordUnknownDeliveryFailure(
                    error_code=error_code,
                    failure_stage=PublicationFailureStage.SEND,
                    anchor_message_id=anchor_message_id,
                ) from error

            message_id = getattr(message, "id", None)
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                raise DiscordUnknownDeliveryFailure(
                    error_code="discord_send_invalid_ack",
                    failure_stage=PublicationFailureStage.SEND,
                    anchor_message_id=anchor_message_id,
                )
            if anchor_message_id is None:
                anchor_message_id = str(message_id)

        if anchor_message_id is None:
            raise RuntimeError("Discord sender completed without an acknowledged page.")
        return DiscordPublicationSendReceipt(anchor_message_id=anchor_message_id)

    async def _resolve_channel(self, channel_id: int) -> object:
        channel = self.client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.client.fetch_channel(channel_id)
        except Exception as error:
            raise DiscordKnownDeliveryFailure(
                error_code=_stable_discord_error_code("channel", error),
                failure_stage=PublicationFailureStage.CHANNEL,
            ) from error


@dataclass(frozen=True, slots=True)
class PublicationDeliveryWorkerConfig:
    """Deployment-owned bounded work and retry timing configuration."""

    max_batch_size: int
    max_attempts: int
    retry_delay: timedelta
    pending_timeout: timedelta

    def __post_init__(self) -> None:
        for field_name in ("max_batch_size", "max_attempts"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")
        for field_name in ("retry_delay", "pending_timeout"):
            value = getattr(self, field_name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{field_name} must be a positive duration.")


@dataclass(frozen=True, slots=True)
class PublicationDeliveryRun:
    """Bounded worker result suitable for runtime metrics/logging."""

    stale_recovered: int
    claimed: int
    sent: int
    failed: int
    delivery_unknown: int


PublicationRenderer = Callable[[str, str, Mapping[str, object]], RenderedDiscordPublication]


@dataclass(frozen=True, slots=True)
class PublicationDeliveryWorker:
    """Run bounded post-commit delivery without holding a database UoW."""

    commands: PublicationDeliveryCommands
    sender: DiscordPublicationSender
    config: PublicationDeliveryWorkerConfig
    run_application: BlockingApplicationRunner = run_blocking_application
    renderer: PublicationRenderer = render_discord_publication

    async def run_once(self) -> PublicationDeliveryRun:
        stale_recovered = await self.run_application(
            lambda: self.commands.recover_stale_pending(
                pending_timeout=self.config.pending_timeout,
                max_count=self.config.max_batch_size,
            )
        )
        claimed_count = 0
        sent_count = 0
        failed_count = 0
        unknown_count = 0

        for _ in range(self.config.max_batch_size):
            claimed = await self.run_application(
                lambda: self.commands.claim_next(
                    retry_delay=self.config.retry_delay,
                    max_attempts=self.config.max_attempts,
                )
            )
            if claimed is None:
                break
            claimed_count += 1

            try:
                rendered = self.renderer(
                    claimed.destination_kind,
                    claimed.event_type,
                    claimed.payload_json,
                )
            except DiscordPublicationRenderError:
                await self._finalize_failed(
                    claimed,
                    error_code="invalid_stored_payload",
                    failure_stage=PublicationFailureStage.RENDER,
                )
                failed_count += 1
                continue
            except Exception:
                logger.exception("Unexpected publication renderer failure publication_id=%s", claimed.publication_id)
                await self._finalize_failed(
                    claimed,
                    error_code="renderer_internal_error",
                    failure_stage=PublicationFailureStage.RENDER,
                )
                failed_count += 1
                continue

            try:
                receipt = await self.sender.send(
                    guild_id=claimed.guild_id,
                    target_channel_id=claimed.target_channel_id,
                    pages=rendered.pages,
                )
            except DiscordKnownDeliveryFailure as error:
                await self._finalize_failed(
                    claimed,
                    error_code=error.error_code,
                    failure_stage=error.failure_stage,
                )
                failed_count += 1
                continue
            except DiscordUnknownDeliveryFailure as error:
                await self._finalize_unknown(
                    claimed,
                    error_code=error.error_code,
                    failure_stage=error.failure_stage,
                    anchor_message_id=error.anchor_message_id,
                )
                unknown_count += 1
                continue
            except Exception:
                logger.exception("Unclassified Discord publication failure publication_id=%s", claimed.publication_id)
                await self._finalize_unknown(
                    claimed,
                    error_code="discord_unclassified_error",
                    failure_stage=PublicationFailureStage.SEND,
                    anchor_message_id=None,
                )
                unknown_count += 1
                continue

            await self._finalize_sent(
                claimed,
                anchor_message_id=receipt.anchor_message_id,
            )
            sent_count += 1

        return PublicationDeliveryRun(
            stale_recovered=stale_recovered,
            claimed=claimed_count,
            sent=sent_count,
            failed=failed_count,
            delivery_unknown=unknown_count,
        )

    async def _finalize_sent(
        self,
        claimed: ClaimedPublication,
        *,
        anchor_message_id: str,
    ) -> None:
        command = FinalizePublication(
            publication_id=claimed.publication_id,
            attempt_count=claimed.attempt_count,
            status=PublicationStatus.SENT,
            discord_message_id=anchor_message_id,
        )
        await self.run_application(lambda: self.commands.finalize(command))

    async def _finalize_failed(
        self,
        claimed: ClaimedPublication,
        *,
        error_code: str,
        failure_stage: PublicationFailureStage,
    ) -> None:
        await self.run_application(
            lambda: self.commands.finalize(
                FinalizePublication(
                    publication_id=claimed.publication_id,
                    attempt_count=claimed.attempt_count,
                    status=PublicationStatus.FAILED,
                    error_code=error_code,
                    failure_stage=failure_stage,
                )
            )
        )

    async def _finalize_unknown(
        self,
        claimed: ClaimedPublication,
        *,
        error_code: str,
        failure_stage: PublicationFailureStage,
        anchor_message_id: str | None,
    ) -> None:
        await self.run_application(
            lambda: self.commands.finalize(
                FinalizePublication(
                    publication_id=claimed.publication_id,
                    attempt_count=claimed.attempt_count,
                    status=PublicationStatus.DELIVERY_UNKNOWN,
                    discord_message_id=anchor_message_id,
                    error_code=error_code,
                    failure_stage=failure_stage,
                )
            )
        )


class PublicationRunOnceWorker(Protocol):
    async def run_once(self) -> PublicationDeliveryRun: ...


@dataclass(frozen=True, slots=True)
class MatchOddsPublicationRuntimeWorker:
    """Generate one due Match odds intent before the shared delivery pass."""

    commands: MatchOddsPublicationCommands
    guild_id: str
    delivery_worker: PublicationRunOnceWorker
    run_application: BlockingApplicationRunner = run_blocking_application

    async def run_once(self) -> PublicationDeliveryRun:
        try:
            refresh = await self.run_application(lambda: self.commands.refresh_due(guild_id=self.guild_id))
            if refresh.publication is not None:
                logger.info(
                    "Periodic Match odds intent committed publication_id=%s mode=%s open_matches=%s",
                    refresh.publication.publication_id,
                    refresh.mode.value,
                    refresh.open_match_count,
                )
        except Exception as exc:
            logger.error(
                "Periodic Match odds generation failed guild_id=%s error_type=%s",
                self.guild_id,
                type(exc).__name__,
            )
        return await self.delivery_worker.run_once()
