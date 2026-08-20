from __future__ import annotations

from umacircle_bot.services.application import run_application_query
from umacircle_bot.services.match_reporting import (
    GameAccountRatingDTO,
    PersonaMatchActivityDTO,
    get_game_account_rating_report,
    get_persona_match_activity_report,
)


def query_persona_match_activity_report() -> tuple[PersonaMatchActivityDTO, ...]:
    return run_application_query(get_persona_match_activity_report)


def query_game_account_rating_report() -> tuple[GameAccountRatingDTO, ...]:
    return run_application_query(get_game_account_rating_report)
