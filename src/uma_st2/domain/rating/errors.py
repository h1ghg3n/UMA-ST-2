"""Rating domain failures."""


class RatingError(ValueError):
    """Base failure for canonical Rating rules."""


class RatingRuleError(RatingError):
    """An immutable Rating rule is malformed or incomplete."""


class RatingCalculationError(RatingError):
    """A participant board cannot produce an unambiguous Rating transition."""
