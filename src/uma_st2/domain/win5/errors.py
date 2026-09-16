"""Domain errors for WIN5 invariant checks."""


class Win5DomainError(ValueError):
    """Raised when a WIN5 domain invariant is violated."""


class Win5RaceInvariantError(Win5DomainError):
    """Raised when WIN5 Round/Race cardinality or membership is invalid."""


class Win5SubmissionInvariantError(Win5DomainError):
    """Raised when WIN5 submission-level invariants are invalid."""


class Win5ResultInvariantError(Win5DomainError):
    """Raised when WIN5 result structure is invalid for an explicit contract."""
