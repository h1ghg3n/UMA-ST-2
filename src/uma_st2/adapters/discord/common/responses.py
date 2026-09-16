"""Mention-safe bounded Discord text helpers."""

from __future__ import annotations

from collections.abc import Sequence

import discord

from ..strings.common import omitted_items_suffix


def safe_discord_text(value: str, *, limit: int = 100) -> str:
    """Escape untrusted text and bound one rendered label."""

    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))[:limit]


def bounded_discord_message(lines: Sequence[str], *, limit: int = 1900) -> str:
    """Join complete lines within a safe message bound and report omissions."""

    selected: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if selected else 0)
        if length + addition > limit:
            while True:
                omitted = len(lines) - len(selected)
                suffix = omitted_items_suffix(omitted)
                prefix = "\n".join(selected)
                candidate = f"{prefix}\n{suffix}" if prefix else suffix
                if len(candidate) <= limit:
                    return candidate
                if not selected:
                    return suffix[:limit]
                removed = selected.pop()
                length -= len(removed) + (1 if selected else 0)
        selected.append(line)
        length += addition
    return "\n".join(selected)


def buttonless_terminal_layout(message: str, *, timeout_seconds: float) -> discord.ui.LayoutView:
    """Render one bounded terminal message without interactive components."""

    view = discord.ui.LayoutView(timeout=timeout_seconds)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(bounded_discord_message((message,), limit=3500))))
    return view
