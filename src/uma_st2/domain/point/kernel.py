"""Pure domain calculations for Circle Point balance updates."""

from __future__ import annotations

from .errors import PointAmountError


def _require_integer(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PointAmountError(f"{field_name} must be an integer.")

    return value


def calculate_next_circle_point_balance(current_balance: int, amount: int) -> int:
    """Calculate the next current balance from authoritative balance + signed amount."""

    current_balance_int = _require_integer(current_balance, field_name="current_balance")
    amount_int = _require_integer(amount, field_name="amount")
    return current_balance_int + amount_int
