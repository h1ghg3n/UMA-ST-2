"""Authorization and response-preparation port for Discord commands."""

from __future__ import annotations

from typing import Protocol

import discord


class PrepareDiscordCommand(Protocol):
    """Authorize one interaction and establish its deferred response visibility.

    A concrete runtime implementation must validate current guild, channel, and
    Role-ID access. It returns ``False`` after delivering a private rejection,
    or defers with the requested visibility and returns ``True``.
    """

    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        ephemeral: bool,
    ) -> bool: ...


class AuthorizeDiscordAutocomplete(Protocol):
    """Authorize autocomplete without acknowledging or deferring the interaction."""

    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool: ...


class AuthorizeDiscordInteraction(Protocol):
    """Authorize a Modal/View transition without acknowledging it on success.

    A concrete runtime implementation returns ``False`` after delivering a
    private rejection. An allowed interaction remains unacknowledged so the
    adapter can use ``send_modal()``, ``send_message()``, or ``edit_message()``
    as its initial response.
    """

    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool: ...
