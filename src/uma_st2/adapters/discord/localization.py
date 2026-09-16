"""Reviewed Discord-native Korean name localization metadata."""

from __future__ import annotations

import discord
from discord import app_commands

_KOREAN_NAME_EXTRA = "korean_name"
_NAME_LOCATIONS = {
    app_commands.TranslationContextLocation.command_name,
    app_commands.TranslationContextLocation.group_name,
    app_commands.TranslationContextLocation.parameter_name,
}


def _korean_name(default: str, korean: str, *, kind: str) -> app_commands.locale_str:
    if not isinstance(default, str) or not default:
        raise ValueError(f"default {kind} name must be a non-empty string.")
    if not isinstance(korean, str) or not korean:
        raise ValueError(f"Korean {kind} name must be a non-empty string.")
    return app_commands.locale_str(default, **{_KOREAN_NAME_EXTRA: korean})


def korean_parameter_name(default: str, korean: str) -> app_commands.locale_str:
    """Attach one reviewed Korean display name to a canonical parameter name."""

    return _korean_name(default, korean, kind="parameter")


def korean_command_name(default: str, korean: str) -> app_commands.locale_str:
    """Attach one reviewed Korean display name to a canonical command/group name."""

    return _korean_name(default, korean, kind="command")


class KoreanParameterNameTranslator(app_commands.Translator):
    """Translate reviewed name metadata without registering aliases."""

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        if locale is not discord.Locale.korean or context.location not in _NAME_LOCATIONS:
            return None
        translated = string.extras.get(_KOREAN_NAME_EXTRA)
        return translated if isinstance(translated, str) and translated else None
