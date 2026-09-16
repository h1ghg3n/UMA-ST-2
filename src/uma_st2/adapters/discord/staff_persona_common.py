"""Shared scalar helpers for the private Staff Persona workflow."""

from __future__ import annotations

from dataclasses import dataclass


def required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive Discord snowflake.")
    return value


def bounded_label(value: str, *, limit: int = 100) -> str:
    normalized = " ".join(str(value).split())
    return normalized[:limit] if normalized else "-"


@dataclass(frozen=True, slots=True)
class StaffPersonaInteractionContext:
    """Opener/guild/channel binding for one private staff workflow."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> StaffPersonaInteractionContext:
        return cls(
            user_id=required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            ),
            guild_id=required_snowflake(
                getattr(interaction, "guild_id", None),
                field_name="guild ID",
            ),
            channel_id=required_snowflake(
                getattr(interaction, "channel_id", None),
                field_name="channel ID",
            ),
        )

    def matches(self, interaction: object) -> bool:
        return (
            getattr(getattr(interaction, "user", None), "id", None) == self.user_id
            and getattr(interaction, "guild_id", None) == self.guild_id
            and getattr(interaction, "channel_id", None) == self.channel_id
        )
