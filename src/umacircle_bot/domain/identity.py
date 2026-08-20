from enum import StrEnum

from umacircle_bot.domain.errors import IdentityStateError, InvalidUmaPidError

UMA_PID_MAX_LENGTH = 32


class IdentityStatus(StrEnum):
    PENDING = "pending_identity"
    CONFIRMED = "confirmed_identity"
    CONFLICT = "identity_conflict"
    CANCELLED = "registration_cancelled"


def normalize_uma_pid(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidUmaPidError("uma_pid must be text")
    normalized = value.strip().replace(" ", "").replace("-", "")
    if not normalized:
        raise InvalidUmaPidError("uma_pid is required")
    if not normalized.isdecimal():
        raise InvalidUmaPidError("uma_pid must contain only digits")
    if len(normalized) > UMA_PID_MAX_LENGTH:
        raise InvalidUmaPidError("uma_pid is too long")
    return normalized


def validate_registration_pid(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidUmaPidError("uma_pid must be text")
    if not 1 <= len(value) <= UMA_PID_MAX_LENGTH or value[0] == "0" or not value.isascii() or not value.isdecimal():
        raise InvalidUmaPidError("uma_pid must be 1 to 32 ASCII digits without a leading zero")
    return value


def is_valid_uma_pid(value: str) -> bool:
    try:
        normalize_uma_pid(value)
    except InvalidUmaPidError:
        return False
    return True


def normalize_identity_status(value: IdentityStatus | str) -> IdentityStatus:
    if isinstance(value, IdentityStatus):
        return value
    if not isinstance(value, str):
        raise IdentityStateError("identity status must be text")
    try:
        return IdentityStatus(value.strip().lower())
    except ValueError as exc:
        raise IdentityStateError("unsupported identity status") from exc


def normalize_identity_state(
    *,
    uma_pid: str | None,
    status: IdentityStatus | str,
) -> tuple[str | None, IdentityStatus]:
    normalized_status = normalize_identity_status(status)
    if normalized_status is IdentityStatus.CONFIRMED:
        if uma_pid is None:
            raise IdentityStateError("confirmed identity requires uma_pid")
        return normalize_uma_pid(uma_pid), normalized_status
    if uma_pid is not None:
        raise IdentityStateError("unresolved identity must not have uma_pid")
    return None, normalized_status
