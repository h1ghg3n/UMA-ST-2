"""Physical SQL types shared by the canonical V2 ORM mappings."""

from sqlalchemy import Enum, String

# MariaDB has no portable UUID column across the supported deployment baseline.
# Persona IDs remain canonical hyphenated UUID strings so transition-safe IDs can
# be preserved without a binary encoding convention.
PERSONA_ID = String(36)

PERSONA_STATUS = Enum(
    "normal",
    "warning",
    "pending_approval",
    "expelled",
    "withdrawn",
    name="persona_status",
)
GAME_REGION = Enum("KR", "JP", name="game_region")
REGISTRATION_REQUEST_STATUS = Enum(
    "pending",
    "approved",
    "cancelled",
    name="registration_request_status",
)
MATCH_GRADE = Enum("G1", "G2", "G3", "LISTED", "OP", name="match_grade")
MATCH_SOURCE_KIND = Enum("native_v2", "imported_v1", name="match_source_kind")
MATCH_STATUS = Enum(
    "scheduled",
    "entry_confirmed",
    "betting_open",
    "betting_closed",
    "result_confirmed",
    "settled",
    "cancelled",
    "voided",
    name="match_status",
)
MATCH_RESULT_SUBMISSION_STATUS = Enum(
    "pending",
    "confirmed",
    "rejected",
    "superseded",
    name="match_result_submission_status",
)
MATCH_RESULT_SOURCE_KIND = Enum("manual", "ocr", name="match_result_source_kind")
MATCH_RATING_DISPOSITION = Enum(
    "rated",
    "excluded",
    "not_applicable",
    name="match_rating_disposition",
)
MATCH_SURFACE = Enum("turf", "dirt", name="match_surface")
MATCH_DIRECTION = Enum("left", "right", "straight", name="match_direction")
STADIUM_COURSE_LAYOUT = Enum(
    "standard",
    "inner",
    "outer",
    "outer_to_inner",
    name="stadium_course_layout",
)
MATCH_SEASON = Enum("spring", "summer", "autumn", "winter", name="match_season")
MATCH_WEATHER = Enum("sunny", "cloudy", "rain", "snow", "random", name="match_weather")
MATCH_TIME_OF_DAY = Enum("day", "night", name="match_time_of_day")
MATCH_TRACK_CONDITION = Enum("firm", "good", "soft", "heavy", "random", name="match_track_condition")
BET_TYPE = Enum("win", "quinella", "trio", name="bet_type")
BET_STATUS = Enum("active", "settled", "cancelled", name="bet_status")
WIN5_SEASON_STATUS = Enum("draft", "active", "closed", "cancelled", name="win5_season_status")
WIN5_ROUND_TYPE = Enum("normal", "special", name="win5_round_type")
WIN5_ROUND_SOURCE_KIND = Enum("native_v2", "imported_v1", name="win5_round_source_kind")
WIN5_ROUND_STATUS = Enum(
    "setup",
    "open",
    "closed",
    "scored",
    "cancelled",
    name="win5_round_status",
)
WIN5_SUBMISSION_TIER = Enum(
    "TOP1",
    "TOP3",
    "TOP5",
    "SPECIAL_WINNER",
    name="win5_submission_tier",
)
WIN5_SUBMISSION_STATUS = Enum("accepted", "cancelled", name="win5_submission_status")
WIN5_JUDGEMENT_OUTCOME = Enum(
    "exact",
    "wrong_position",
    "off_board",
    "missing",
    "void",
    name="win5_judgement_outcome",
)
PUBLICATION_STATUS = Enum(
    "suppressed",
    "awaiting_channel",
    "ready",
    "pending",
    "sent",
    "failed",
    "delivery_unknown",
    name="publication_status",
)
MATCH_ODDS_REFRESH_MODE = Enum("normal", "live", name="match_odds_refresh_mode")
