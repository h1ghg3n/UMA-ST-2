"""Domain errors for Circle Point invariants and amount semantics."""


class PointDomainError(ValueError):
    """Raised when a Circle Point domain invariant is violated."""


class CirclePointInvariantError(PointDomainError):
    """Raised when Circle Point state fails validation."""


class PointAmountError(PointDomainError):
    """Raised when a signed amount or balance value is invalid."""
