"""Shared provider-independent durable publication intent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from uma_st2.domain.publication import PublicationStatus


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "optional " if optional else ""
        raise ValueError(f"{field_name} must be a non-empty {qualifier}string no longer than {max_length} characters.")


def publication_payload_fingerprint(payload: dict[str, object]) -> str:
    """Return the canonical JSON fingerprint shared by every publication type."""

    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicationIntent:
    """One durable provider-independent logical publication to insert atomically."""

    guild_id: str
    destination_kind: str
    event_type: str
    event_key: str
    source_kind: str
    source_id: int
    target_channel_id: str | None
    payload_json: dict[str, object]
    payload_fingerprint: str
    status: PublicationStatus

    def __post_init__(self) -> None:
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32)
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        _require_bounded_string(self.event_type, field_name="event_type", max_length=64)
        _require_bounded_string(self.event_key, field_name="event_key", max_length=128)
        _require_bounded_string(self.source_kind, field_name="source_kind", max_length=32)
        _require_positive_int(self.source_id, field_name="source_id")
        _require_bounded_string(
            self.target_channel_id,
            field_name="target_channel_id",
            max_length=32,
            optional=True,
        )
        if not isinstance(self.payload_json, dict):
            raise ValueError("payload_json must be a dictionary.")
        if self.payload_fingerprint != publication_payload_fingerprint(self.payload_json):
            raise ValueError("payload_fingerprint does not match payload_json.")
        object.__setattr__(self, "status", PublicationStatus(self.status))
