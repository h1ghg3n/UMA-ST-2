"""Shared Discord adapter execution and response primitives."""

from .bridge import BlockingApplicationRunner, run_blocking_application
from .delivery import (
    correlation_id,
    send_deferred_attachment_safely,
    send_deferred_response_safely,
    send_ephemeral_internal_error_after_defer_safely,
    send_private_error_after_defer_safely,
    send_private_response_after_public_defer_safely,
)
from .preparation import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    PrepareDiscordCommand,
)
from .responses import bounded_discord_message, buttonless_terminal_layout, safe_discord_text

__all__ = [
    "BlockingApplicationRunner",
    "AuthorizeDiscordAutocomplete",
    "AuthorizeDiscordInteraction",
    "PrepareDiscordCommand",
    "buttonless_terminal_layout",
    "bounded_discord_message",
    "correlation_id",
    "run_blocking_application",
    "safe_discord_text",
    "send_deferred_attachment_safely",
    "send_deferred_response_safely",
    "send_ephemeral_internal_error_after_defer_safely",
    "send_private_error_after_defer_safely",
    "send_private_response_after_public_defer_safely",
]
