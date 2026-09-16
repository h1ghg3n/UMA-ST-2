"""Canonical enumerations for identity domain values."""

from enum import StrEnum


class PersonaStatus(StrEnum):
    """Canonical persona lifecycle status values."""

    NORMAL = "normal"
    WARNING = "warning"
    PENDING_APPROVAL = "pending_approval"
    EXPELLED = "expelled"
    WITHDRAWN = "withdrawn"


class GameRegion(StrEnum):
    """Canonical identity game region values."""

    KR = "KR"
    JP = "JP"


class RegistrationRequestStatus(StrEnum):
    """Canonical onboarding request status values."""

    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"
