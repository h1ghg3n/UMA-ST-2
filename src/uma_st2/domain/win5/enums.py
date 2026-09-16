"""Canonical enumerations for WIN5 domain values."""

from enum import StrEnum


class Win5SeasonStatus(StrEnum):
    """Canonical WIN5 season statuses."""

    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Win5RoundType(StrEnum):
    """Canonical WIN5 round types."""

    NORMAL = "normal"
    SPECIAL = "special"


class Win5RoundSourceKind(StrEnum):
    """Canonical provenance of a WIN5 Round graph."""

    NATIVE_V2 = "native_v2"
    IMPORTED_V1 = "imported_v1"


class Win5RoundStatus(StrEnum):
    """Canonical WIN5 round workflow status vocabulary."""

    SETUP = "setup"
    OPEN = "open"
    CLOSED = "closed"
    SCORED = "scored"
    CANCELLED = "cancelled"


class Win5SubmissionTier(StrEnum):
    """Canonical WIN5 submission tier values."""

    TOP1 = "TOP1"
    TOP3 = "TOP3"
    TOP5 = "TOP5"
    SPECIAL_WINNER = "SPECIAL_WINNER"


class Win5SubmissionStatus(StrEnum):
    """Canonical WIN5 submission workflow statuses."""

    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


class Win5JudgementOutcome(StrEnum):
    """Persisted Normal-position and Special-Race judgement outcomes."""

    EXACT = "exact"
    WRONG_POSITION = "wrong_position"
    OFF_BOARD = "off_board"
    MISSING = "missing"
    VOID = "void"
