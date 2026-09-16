"""Domain errors for MatchEntry invariants."""


class MatchEntryInvariantError(ValueError):
    """Raised when a MatchEntry-related invariant is violated."""


class MatchEntryCountError(MatchEntryInvariantError):
    """Raised when a Match contains more than the supported Entry count."""


class MatchEntryRegionError(MatchEntryInvariantError):
    """Raised when MatchEntry game regions are mixed within a Match."""


class MatchEntryVariantError(MatchEntryInvariantError):
    """Raised when variant/umamusume relationship is invalid."""


class MatchEntryRankError(MatchEntryInvariantError):
    """Raised when a MatchEntry rank invariant is violated."""


class MatchEntryMarginError(MatchEntryInvariantError):
    """Raised when a source-display finish margin is invalid."""


class MatchEntryPopularityRankError(MatchEntryInvariantError):
    """Raised when popularity rank invariant is violated."""
