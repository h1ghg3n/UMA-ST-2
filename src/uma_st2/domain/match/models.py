"""Canonical Circle Match vocabulary shared by application boundaries."""

from __future__ import annotations

from enum import StrEnum


class MatchGrade(StrEnum):
    """Rating and reward policy grade selected at Match creation."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    LISTED = "LISTED"
    OP = "OP"


class MatchSourceKind(StrEnum):
    """Immutable native/import discriminator for one Match."""

    NATIVE_V2 = "native_v2"
    IMPORTED_V1 = "imported_v1"


class MatchStatus(StrEnum):
    """Canonical Match lifecycle states."""

    SCHEDULED = "scheduled"
    ENTRY_CONFIRMED = "entry_confirmed"
    BETTING_OPEN = "betting_open"
    BETTING_CLOSED = "betting_closed"
    RESULT_CONFIRMED = "result_confirmed"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    VOIDED = "voided"


class MatchResultSubmissionStatus(StrEnum):
    """Lifecycle values for one versioned result candidate."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class MatchResultSourceKind(StrEnum):
    """Provider-independent source classification for a result candidate."""

    MANUAL = "manual"
    OCR = "ocr"


class MatchRatingDisposition(StrEnum):
    """Terminal Rating participation decision for one official MatchEntry."""

    RATED = "rated"
    EXCLUDED = "excluded"
    NOT_APPLICABLE = "not_applicable"


class MatchSeason(StrEnum):
    """Canonical race-environment season."""

    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"


class MatchWeather(StrEnum):
    """Canonical race-environment weather."""

    SUNNY = "sunny"
    CLOUDY = "cloudy"
    RAIN = "rain"
    SNOW = "snow"
    RANDOM = "random"


class MatchTimeOfDay(StrEnum):
    """Canonical race-environment time of day."""

    DAY = "day"
    NIGHT = "night"


class MatchTrackCondition(StrEnum):
    """Canonical race-environment track condition."""

    FIRM = "firm"
    GOOD = "good"
    SOFT = "soft"
    HEAVY = "heavy"
    RANDOM = "random"


class MatchSurface(StrEnum):
    """Master-course surface vocabulary."""

    TURF = "turf"
    DIRT = "dirt"


class MatchDirection(StrEnum):
    """Master-course direction vocabulary."""

    LEFT = "left"
    RIGHT = "right"
    STRAIGHT = "straight"


class StadiumCourseLayout(StrEnum):
    """Master-course layout vocabulary."""

    STANDARD = "standard"
    INNER = "inner"
    OUTER = "outer"
    OUTER_TO_INNER = "outer_to_inner"
