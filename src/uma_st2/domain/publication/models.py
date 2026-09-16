"""Minimal canonical publication state vocabulary."""

from enum import StrEnum


class PublicationStatus(StrEnum):
    """Current durable delivery state for one logical publication."""

    SUPPRESSED = "suppressed"
    AWAITING_CHANNEL = "awaiting_channel"
    READY = "ready"
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"
