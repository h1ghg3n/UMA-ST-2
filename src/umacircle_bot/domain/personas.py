from uuid import UUID

from umacircle_bot.domain.errors import IdentityStateError


def normalize_persona_id(value: str) -> str:
    if not isinstance(value, str):
        raise IdentityStateError("Persona ID must be text")
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise IdentityStateError("Persona ID must be a UUID") from exc


def persona_short_id(value: str) -> str:
    return normalize_persona_id(value).replace("-", "")[:8]
