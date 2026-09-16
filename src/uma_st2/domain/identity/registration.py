"""Native registration-input rules kept separate from historical stored facts."""

from __future__ import annotations

from typing import Final

from .errors import RegistrationRequestInvariantError

UMA_PID_MAX_LENGTH: Final = 32


def normalize_registration_pid(value: str) -> str:
    """Normalize surrounding whitespace and require one canonical numeric PID."""

    if not isinstance(value, str):
        raise RegistrationRequestInvariantError("uma_pid must be text.")
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= UMA_PID_MAX_LENGTH
        or normalized.startswith("0")
        or not normalized.isascii()
        or not normalized.isdecimal()
    ):
        raise RegistrationRequestInvariantError("uma_pid must be 1 to 32 ASCII digits without a leading zero.")
    return normalized
