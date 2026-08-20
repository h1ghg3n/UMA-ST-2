from umacircle_bot.db.models import Persona


def clear_transitional_main_game_account_pointer(
    *,
    persona: Persona,
    removed_game_account_id: int,
) -> None:
    """Prevent a compatibility pointer from retaining a detached peer account."""

    if persona.main_game_account_id == removed_game_account_id:
        persona.main_game_account_id = None
