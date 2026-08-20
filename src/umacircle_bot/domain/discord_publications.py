import json
from collections.abc import Mapping, Sequence
from enum import StrEnum

from umacircle_bot.domain.errors import DiscordPublicationError

MAX_PUBLICATION_PAYLOAD_BYTES = 16_384
MAX_PUBLICATION_PAYLOAD_DEPTH = 6
FORBIDDEN_PAYLOAD_KEY_PARTS = ("pid", "account_id", "reason")


class DiscordPublicationDestination(StrEnum):
    WIN5_ANNOUNCEMENT = "win5_announcement"
    ROOM_MATCH_ANNOUNCEMENT = "room_match_announcement"
    LOG_MIRROR = "log_mirror"


class DiscordPublicationStatus(StrEnum):
    SUPPRESSED = "suppressed"
    AWAITING_CHANNEL = "awaiting_channel"
    READY = "ready"
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


def validate_safe_publication_payload(
    value: object,
    *,
    destination_kind: str | DiscordPublicationDestination,
) -> dict[str, object]:
    try:
        destination = DiscordPublicationDestination(destination_kind)
    except (TypeError, ValueError) as exc:
        raise DiscordPublicationError("Discord publication destination is invalid") from exc
    if not isinstance(value, Mapping):
        raise DiscordPublicationError("Discord publication payload must be an object")
    normalized = _normalize(value, depth=0, destination=destination)
    encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PUBLICATION_PAYLOAD_BYTES:
        raise DiscordPublicationError("Discord publication payload is too large")
    return normalized


def _normalize(value: object, *, depth: int, destination: DiscordPublicationDestination):
    if depth > MAX_PUBLICATION_PAYLOAD_DEPTH:
        raise DiscordPublicationError("Discord publication payload is too deeply nested")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise DiscordPublicationError("Discord publication payload key is invalid")
            lowered = key.lower()
            if any(part in lowered for part in FORBIDDEN_PAYLOAD_KEY_PARTS):
                raise DiscordPublicationError("Discord publication payload contains a forbidden field")
            if "actor" in lowered or "discord_user_id" in lowered:
                if destination is not DiscordPublicationDestination.LOG_MIRROR or key != "actor_discord_user_id":
                    raise DiscordPublicationError("Discord publication payload contains a forbidden identity field")
            result[key] = _normalize(item, depth=depth + 1, destination=destination)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 100:
            raise DiscordPublicationError("Discord publication payload list is too large")
        return [_normalize(item, depth=depth + 1, destination=destination) for item in value]
    raise DiscordPublicationError("Discord publication payload contains an unsupported value")
