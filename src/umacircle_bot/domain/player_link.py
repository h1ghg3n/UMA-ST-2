from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_player_name_strict(value: str) -> str:
    """Normalize a display name without discarding meaningful characters."""

    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


def normalize_player_name_relaxed(value: str) -> str:
    """Produce a search-only key that removes whitespace and decoration."""

    return "".join(character for character in normalize_player_name_strict(value) if character.isalnum())


def player_name_similarity(left: str, right: str) -> float:
    """Return a deterministic standard-library similarity score for candidate ordering."""

    return SequenceMatcher(a=normalize_player_name_strict(left), b=normalize_player_name_strict(right)).ratio()
