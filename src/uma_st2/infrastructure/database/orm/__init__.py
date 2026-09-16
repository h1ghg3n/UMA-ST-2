"""Canonical V2 SQLAlchemy ORM metadata."""

from .base import Base
from .betting import BetORM
from .identity import DiscordAccountORM, GameAccountORM, GameAccountRegistrationRequestORM, PersonaORM
from .master_data import StadiumCourseORM, StadiumORM, UmamusumeORM, UmamusumeVariantORM
from .match import MatchConditionORM, MatchEntryORM, MatchORM, MatchResultSubmissionORM
from .operations import (
    BetOperationORM,
    IdentityOperationORM,
    MatchOperationORM,
    OperationORM,
    SettingsOperationORM,
    Win5OperationORM,
)
from .point import CirclePointORM, PointTransactionORM
from .publication import BotGuildSettingORM, DiscordPublicationORM
from .rating import RatingORM, RatingRuleORM, RatingRuleVersionORM, RatingTransactionORM
from .win5 import (
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventItemORM,
    Win5ScoreEventORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)

__all__ = [
    "Base",
    "BetORM",
    "BetOperationORM",
    "BotGuildSettingORM",
    "CirclePointORM",
    "DiscordAccountORM",
    "DiscordPublicationORM",
    "GameAccountORM",
    "GameAccountRegistrationRequestORM",
    "IdentityOperationORM",
    "MatchConditionORM",
    "MatchEntryORM",
    "MatchORM",
    "MatchOperationORM",
    "MatchResultSubmissionORM",
    "OperationORM",
    "PersonaORM",
    "PointTransactionORM",
    "RatingORM",
    "RatingRuleORM",
    "RatingRuleVersionORM",
    "RatingTransactionORM",
    "SettingsOperationORM",
    "StadiumCourseORM",
    "StadiumORM",
    "UmamusumeORM",
    "UmamusumeVariantORM",
    "Win5OperationORM",
    "Win5RaceEntryORM",
    "Win5RaceORM",
    "Win5ResultORM",
    "Win5RoundORM",
    "Win5ScoreEventItemORM",
    "Win5ScoreEventORM",
    "Win5ScoreORM",
    "Win5SeasonORM",
    "Win5SubmissionORM",
    "Win5SubmissionPickORM",
]
