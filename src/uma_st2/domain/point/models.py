"""Circle Point aggregate and deterministic balance mutation methods."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import CirclePointInvariantError, PointAmountError
from .kernel import calculate_next_circle_point_balance


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise CirclePointInvariantError(f"{field_name} must be a string.")

    return value


def _require_integer(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PointAmountError(f"{field_name} must be an integer.")

    return value


@dataclass(frozen=True, slots=True)
class CirclePoint:
    """Canonical current Circle Point view per Persona."""

    persona_id: str
    balance: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _require_string(self.persona_id, field_name="persona_id"))
        object.__setattr__(self, "balance", _require_integer(self.balance, field_name="balance"))

    def apply_delta(self, amount: int) -> CirclePoint:
        """Return a new CirclePoint with balance mutated by signed amount."""

        return replace(self, balance=calculate_next_circle_point_balance(self.balance, amount))
