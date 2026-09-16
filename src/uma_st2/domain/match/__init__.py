"""Match domain package."""

from .errors import (
    MatchEntryCountError,
    MatchEntryInvariantError,
    MatchEntryMarginError,
    MatchEntryPopularityRankError,
    MatchEntryRankError,
    MatchEntryRegionError,
    MatchEntryVariantError,
)
from .invariants import (
    MATCH_ENTRY_MAXIMUM_COUNT,
    MatchEntryCandidate,
    validate_match_entry_invariants,
)
from .models import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from .settlement import (
    PLACEMENT_REWARD_RULE_VERSION,
    calculate_match_placement_reward,
)

__all__ = [
    "MATCH_ENTRY_MAXIMUM_COUNT",
    "MatchEntryCandidate",
    "MatchEntryCountError",
    "MatchEntryInvariantError",
    "MatchEntryMarginError",
    "MatchEntryPopularityRankError",
    "MatchEntryRegionError",
    "MatchEntryRankError",
    "MatchEntryVariantError",
    "MatchDirection",
    "MatchGrade",
    "MatchRatingDisposition",
    "MatchResultSourceKind",
    "MatchResultSubmissionStatus",
    "MatchSeason",
    "MatchSourceKind",
    "MatchStatus",
    "MatchSurface",
    "MatchTimeOfDay",
    "MatchTrackCondition",
    "MatchWeather",
    "PLACEMENT_REWARD_RULE_VERSION",
    "StadiumCourseLayout",
    "calculate_match_placement_reward",
    "validate_match_entry_invariants",
]
