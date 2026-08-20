from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """Discord identity and location bound to a component interaction flow."""

    user_id: int
    guild_id: int
    channel_id: int
