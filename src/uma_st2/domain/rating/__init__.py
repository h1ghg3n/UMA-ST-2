"""GameAccount Rating rules and pure calculations."""

from .calculator import (
    RATING_FORMULA_VERSION,
    RATING_RULE_GRADES,
    RATING_STORAGE_QUANTUM,
    RatingParticipant,
    RatingRule,
    RatingTransactionDraft,
    calculate_rating_transactions,
    normalize_rating_storage,
)
from .errors import RatingCalculationError, RatingError, RatingRuleError

__all__ = [
    "RATING_FORMULA_VERSION",
    "RATING_RULE_GRADES",
    "RATING_STORAGE_QUANTUM",
    "RatingCalculationError",
    "RatingError",
    "RatingParticipant",
    "RatingRule",
    "RatingRuleError",
    "RatingTransactionDraft",
    "calculate_rating_transactions",
    "normalize_rating_storage",
]
