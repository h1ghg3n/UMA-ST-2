from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any
from uuid import uuid4

import discord

from umacircle_bot.discord_channel_provisioning import (
    ManagedChannelKind,
    resolve_or_create_default_channel,
)
from umacircle_bot.domain.discord_publications import (
    DiscordPublicationDestination,
    DiscordPublicationStatus,
    validate_safe_publication_payload,
)
from umacircle_bot.domain.errors import DiscordPublicationConflictError
from umacircle_bot.services.application import run_application_transaction
from umacircle_bot.services.discord_publications import (
    BindDiscordPublicationChannelCommand,
    ClaimDiscordPublicationCommand,
    DiscordPublicationDTO,
    MarkDiscordPublicationChannelFailedCommand,
    RecordDiscordPublicationOutcomeCommand,
    bind_discord_publication_channel,
    claim_discord_publication,
    get_discord_publication,
    mark_discord_publication_channel_failed,
    record_discord_publication_outcome,
)
from umacircle_bot.services.guild_discord_settings import (
    BindProvisionedChannelCommand,
    bind_provisioned_channel_if_unset,
)

DELIVERY_ACTOR = "discord-delivery"
MAX_DELIVERY_MESSAGE_LENGTH = 1900

Renderer = Callable[[Mapping[str, object]], str]


async def deliver_discord_publication_once(
    publication: DiscordPublicationDTO,
    *,
    guild: discord.Guild,
    configured_channel_id: str | None,
    staff_role_ids: tuple[int, ...] = (),
) -> DiscordPublicationDTO:
    """Deliver one committed publication without scanning or automatic unknown retries."""

    current = await asyncio.to_thread(
        _get_publication,
        publication.id,
    )
    if str(guild.id) != current.guild_id:
        raise ValueError("publication guild does not match the Discord guild")
    status = DiscordPublicationStatus(current.status)
    if status in {
        DiscordPublicationStatus.SUPPRESSED,
        DiscordPublicationStatus.PENDING,
        DiscordPublicationStatus.SENT,
        DiscordPublicationStatus.DELIVERY_UNKNOWN,
    }:
        return current

    channel: discord.TextChannel | None = None
    if status is DiscordPublicationStatus.AWAITING_CHANNEL:
        current, channel = await _provision_and_bind_channel(
            current,
            guild=guild,
            configured_channel_id=configured_channel_id,
            staff_role_ids=staff_role_ids,
        )
        if current.status != DiscordPublicationStatus.READY.value:
            return current
    elif status is DiscordPublicationStatus.FAILED and current.failure_stage != "send":
        return current

    if current.target_channel_id is None:
        return current
    if channel is None:
        channel = _configured_text_channel(guild, current.target_channel_id)

    try:
        claimed = await asyncio.to_thread(_claim_publication, current)
    except DiscordPublicationConflictError:
        return await asyncio.to_thread(_get_publication, current.id)
    if channel is None:
        return await asyncio.to_thread(
            _record_outcome,
            claimed,
            DiscordPublicationStatus.FAILED,
            None,
            "TARGET_CHANNEL_UNAVAILABLE",
        )

    try:
        content = render_discord_publication(claimed)
    except Exception:
        return await asyncio.to_thread(
            _record_outcome,
            claimed,
            DiscordPublicationStatus.FAILED,
            None,
            "INVALID_PUBLICATION_PAYLOAD",
        )
    if content is None:
        return await asyncio.to_thread(
            _record_outcome,
            claimed,
            DiscordPublicationStatus.FAILED,
            None,
            "UNKNOWN_EVENT_TYPE",
        )

    try:
        message = await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        outcome, error_code = _classify_send_error(exc)
        return await asyncio.to_thread(
            _record_outcome,
            claimed,
            outcome,
            None,
            error_code,
        )
    return await asyncio.to_thread(
        _record_outcome,
        claimed,
        DiscordPublicationStatus.SENT,
        str(message.id),
        None,
    )


async def _provision_and_bind_channel(
    publication: DiscordPublicationDTO,
    *,
    guild: discord.Guild,
    configured_channel_id: str | None,
    staff_role_ids: tuple[int, ...],
) -> tuple[DiscordPublicationDTO, discord.TextChannel | None]:
    channel_kind = _managed_channel_kind(publication.destination_kind)
    channel = await resolve_or_create_default_channel(
        guild,
        configured_channel_id=configured_channel_id,
        kind=channel_kind,
        staff_role_ids=staff_role_ids,
    )
    if channel is None:
        failed = await asyncio.to_thread(
            _mark_channel_failed,
            publication,
            "CHANNEL_UNAVAILABLE",
        )
        return failed, None

    settings_mutation = await asyncio.to_thread(
        _bind_provisioned_setting,
        publication,
        str(channel.id),
    )
    winner_channel_id = _settings_channel_id(
        settings_mutation.settings,
        publication.destination_kind,
    )
    if winner_channel_id is None:
        failed = await asyncio.to_thread(
            _mark_channel_failed,
            publication,
            "CHANNEL_BIND_FAILED",
        )
        return failed, None

    if str(channel.id) != winner_channel_id:
        channel = await resolve_or_create_default_channel(
            guild,
            configured_channel_id=winner_channel_id,
            kind=channel_kind,
            staff_role_ids=staff_role_ids,
        )
        if channel is None:
            failed = await asyncio.to_thread(
                _mark_channel_failed,
                publication,
                "WINNER_CHANNEL_UNAVAILABLE",
            )
            return failed, None
    bound = await asyncio.to_thread(
        _bind_publication_channel,
        publication,
        winner_channel_id,
    )
    return bound, channel


def render_discord_publication(publication: DiscordPublicationDTO) -> str | None:
    payload = _parse_canonical_payload(publication)
    if publication.destination_kind == DiscordPublicationDestination.LOG_MIRROR.value:
        renderer = _render_log_mirror
    else:
        renderer = RENDERERS.get(publication.event_type)
        if RENDERER_DESTINATIONS.get(publication.event_type) != publication.destination_kind:
            renderer = None
    if renderer is None:
        return None
    content = renderer(payload)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("publication renderer returned an empty message")
    return content[:MAX_DELIVERY_MESSAGE_LENGTH]


def _render_win5_round_open(payload: Mapping[str, object]) -> str:
    _require_exact_keys(payload, {"round_id", "label"})
    round_id = _positive_int(payload, "round_id")
    label = _safe_text(payload.get("label"), fallback="WIN5 라운드")
    return f"WIN5 라운드 #{round_id} {label}이(가) 열렸습니다."


def _render_match_result_confirmed(payload: Mapping[str, object]) -> str:
    _require_exact_keys(
        payload,
        {"race_id", "race_name", "match_type", "ranked_entries"},
    )
    race_id = _positive_int(payload, "race_id")
    race_name = _safe_text(payload.get("race_name"), fallback="룸매치")
    match_type = _safe_text(payload.get("match_type"), fallback="room_match")
    raw_results = payload.get("ranked_entries")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("room-match result payload is missing results")
    lines = [f"룸매치 결과 확정 · #{race_id} {race_name} · {match_type}"]
    for raw_line in raw_results:
        if not isinstance(raw_line, Mapping):
            raise ValueError("room-match result line is invalid")
        required_keys = {"rank", "entry_number", "player_display_name", "character_name"}
        optional_keys = {
            "character_evaluation_rank",
            "popularity_rank",
            "finish_time_ms",
            "finish_margin_text",
        }
        if not required_keys <= set(raw_line) or not set(raw_line) <= required_keys | optional_keys:
            raise ValueError("room-match result line fields do not match the renderer contract")
        rank = _positive_int(raw_line, "rank")
        entry_number = _positive_int(raw_line, "entry_number")
        player_name = _safe_text(
            raw_line.get("player_display_name"),
            fallback=f"엔트리 #{entry_number}",
        )
        character_name = _safe_text(
            raw_line.get("character_name"),
            fallback="말 이름 미등록",
        )
        lines.append(f"{rank}위 · #{entry_number} {player_name} · {character_name}")
        details: list[str] = []
        if "character_evaluation_rank" in raw_line:
            details.append(
                f"평가 {_safe_text(raw_line['character_evaluation_rank'], fallback='-')}",
            )
        if "popularity_rank" in raw_line:
            details.append(f"인기 {_positive_int(raw_line, 'popularity_rank')}위")
        if "finish_time_ms" in raw_line:
            details.append(f"{_positive_int(raw_line, 'finish_time_ms')}ms")
        if "finish_margin_text" in raw_line:
            details.append(
                f"착차 {_safe_text(raw_line['finish_margin_text'], fallback='-')}",
            )
        if details:
            lines.append("  " + " · ".join(details))
    return _bounded_lines(lines)


def _render_log_mirror(payload: Mapping[str, object]) -> str:
    allowed_keys = {
        "command_name",
        "resource_kind",
        "resource_id",
        "actor_discord_user_id",
        "audit_id",
        "correlation_id",
    }
    if not set(payload) <= allowed_keys:
        raise ValueError("log mirror payload contains an unsupported field")
    command = _safe_text(payload.get("command_name"), fallback="unknown")
    actor = _safe_text(payload.get("actor_discord_user_id"), fallback="unknown")
    lines = [f"command={command}", f"actor={actor}"]
    for key in ("audit_id", "resource_kind", "resource_id", "correlation_id"):
        if key in payload:
            lines.append(f"{key}={_safe_text(payload[key], fallback='-')}")
    return _bounded_lines(lines)


RENDERERS: dict[str, Renderer] = {
    "win5_round_open": _render_win5_round_open,
    "room_match_result_confirmed": _render_match_result_confirmed,
}
RENDERER_DESTINATIONS = {
    "win5_round_open": DiscordPublicationDestination.WIN5_ANNOUNCEMENT.value,
    "room_match_result_confirmed": DiscordPublicationDestination.ROOM_MATCH_ANNOUNCEMENT.value,
}


def _parse_canonical_payload(publication: DiscordPublicationDTO) -> dict[str, object]:
    decoded = json.loads(publication.payload_json)
    payload = validate_safe_publication_payload(
        decoded,
        destination_kind=publication.destination_kind,
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical != publication.payload_json:
        raise ValueError("publication payload is not canonical")
    fingerprint = sha256(canonical.encode()).hexdigest()
    if fingerprint != publication.payload_fingerprint:
        raise ValueError("publication payload fingerprint does not match")
    return payload


def _bind_provisioned_setting(publication: DiscordPublicationDTO, channel_id: str):
    return run_application_transaction(
        lambda session: bind_provisioned_channel_if_unset(
            session,
            command=BindProvisionedChannelCommand(
                guild_id=publication.guild_id,
                channel_kind=publication.destination_kind,
                provisioned_channel_id=channel_id,
                actor_discord_user_id=DELIVERY_ACTOR,
                idempotency_key=f"delivery:{publication.id}:settings-bind",
            ),
        )
    )


def _bind_publication_channel(
    publication: DiscordPublicationDTO,
    channel_id: str,
) -> DiscordPublicationDTO:
    return run_application_transaction(
        lambda session: bind_discord_publication_channel(
            session,
            command=BindDiscordPublicationChannelCommand(
                publication_id=publication.id,
                target_channel_id=channel_id,
                actor_discord_user_id=DELIVERY_ACTOR,
                idempotency_key=f"delivery:{publication.id}:publication-bind",
            ),
        )
    )


def _mark_channel_failed(
    publication: DiscordPublicationDTO,
    error_code: str,
) -> DiscordPublicationDTO:
    return run_application_transaction(
        lambda session: mark_discord_publication_channel_failed(
            session,
            command=MarkDiscordPublicationChannelFailedCommand(
                publication_id=publication.id,
                error_code=error_code,
                actor_discord_user_id=DELIVERY_ACTOR,
                idempotency_key=f"delivery:{publication.id}:channel-failed",
            ),
        )
    )


def _claim_publication(publication: DiscordPublicationDTO) -> DiscordPublicationDTO:
    return run_application_transaction(
        lambda session: claim_discord_publication(
            session,
            command=ClaimDiscordPublicationCommand(
                publication_id=publication.id,
                actor_discord_user_id=DELIVERY_ACTOR,
                idempotency_key=f"delivery:{publication.id}:claim:{uuid4().hex}",
            ),
        )
    )


def _get_publication(publication_id: int) -> DiscordPublicationDTO:
    return run_application_transaction(
        lambda session: get_discord_publication(
            session,
            publication_id=publication_id,
        )
    )


def _record_outcome(
    publication: DiscordPublicationDTO,
    outcome: DiscordPublicationStatus,
    discord_message_id: str | None,
    error_code: str | None,
) -> DiscordPublicationDTO:
    return run_application_transaction(
        lambda session: record_discord_publication_outcome(
            session,
            command=RecordDiscordPublicationOutcomeCommand(
                publication_id=publication.id,
                outcome=outcome.value,
                actor_discord_user_id=DELIVERY_ACTOR,
                idempotency_key=(f"delivery:{publication.id}:outcome:{publication.attempt_count}"),
                discord_message_id=discord_message_id,
                error_code=error_code,
            ),
        )
    )


def _configured_text_channel(
    guild: discord.Guild,
    channel_id: str,
) -> discord.TextChannel | None:
    try:
        identifier = int(channel_id)
    except (TypeError, ValueError):
        return None
    channel = guild.get_channel(identifier)
    if channel is None or getattr(channel, "type", None) is not discord.ChannelType.text:
        return None
    return channel


def _managed_channel_kind(destination_kind: str) -> ManagedChannelKind:
    destination = DiscordPublicationDestination(destination_kind)
    return {
        DiscordPublicationDestination.WIN5_ANNOUNCEMENT: ManagedChannelKind.WIN5_ANNOUNCEMENT,
        DiscordPublicationDestination.ROOM_MATCH_ANNOUNCEMENT: ManagedChannelKind.ROOM_MATCH_ANNOUNCEMENT,
        DiscordPublicationDestination.LOG_MIRROR: ManagedChannelKind.LOG,
    }[destination]


def _settings_channel_id(settings: Any, destination_kind: str) -> str | None:
    destination = DiscordPublicationDestination(destination_kind)
    return {
        DiscordPublicationDestination.WIN5_ANNOUNCEMENT: settings.win5_announcement_channel_id,
        DiscordPublicationDestination.ROOM_MATCH_ANNOUNCEMENT: settings.room_match_announcement_channel_id,
        DiscordPublicationDestination.LOG_MIRROR: settings.log_channel_id,
    }[destination]


def _classify_send_error(
    error: Exception,
) -> tuple[DiscordPublicationStatus, str]:
    if isinstance(error, discord.Forbidden):
        return DiscordPublicationStatus.FAILED, "DISCORD_FORBIDDEN"
    if isinstance(error, discord.NotFound):
        return DiscordPublicationStatus.FAILED, "DISCORD_NOT_FOUND"
    status = getattr(error, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return DiscordPublicationStatus.FAILED, f"DISCORD_HTTP_{status}"
    if isinstance(error, TimeoutError):
        return DiscordPublicationStatus.DELIVERY_UNKNOWN, "DISCORD_TIMEOUT"
    if isinstance(error, OSError):
        return DiscordPublicationStatus.DELIVERY_UNKNOWN, "DISCORD_OS_ERROR"
    if isinstance(status, int) and status >= 500:
        return DiscordPublicationStatus.DELIVERY_UNKNOWN, f"DISCORD_HTTP_{status}"
    return DiscordPublicationStatus.DELIVERY_UNKNOWN, "DISCORD_SEND_UNKNOWN"


def _positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _require_exact_keys(
    payload: Mapping[str, object],
    expected: set[str],
) -> None:
    if set(payload) != expected:
        raise ValueError("publication payload fields do not match the renderer contract")


def _safe_text(value: object, *, fallback: str) -> str:
    if value is None:
        value = fallback
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("publication display text is invalid")
    normalized = " ".join(str(value).split())
    if not normalized:
        normalized = fallback
    return discord.utils.escape_markdown(discord.utils.escape_mentions(normalized))[:100]


def _bounded_lines(lines: list[str]) -> str:
    selected: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if selected else 0)
        if length + addition > MAX_DELIVERY_MESSAGE_LENGTH:
            break
        selected.append(line)
        length += addition
    return "\n".join(selected)
