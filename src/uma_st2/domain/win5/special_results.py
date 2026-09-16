"""Pure Special WIN5 authoritative-result fingerprint rules."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .errors import Win5ResultInvariantError


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class Win5SpecialResultWinner:
    """One persisted authoritative position-1 winner for a Special Race."""

    id: int
    race_id: int
    gate_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.gate_number, field_name="gate_number")


def _ordered_valid_result(
    winners: tuple[Win5SpecialResultWinner, ...],
) -> tuple[Win5SpecialResultWinner, ...]:
    ordered = tuple(sorted(winners, key=lambda winner: winner.race_id))
    if not ordered:
        raise Win5ResultInvariantError("Special result requires at least one Race winner.")
    if len({winner.id for winner in ordered}) != len(ordered):
        raise Win5ResultInvariantError("Special result winner IDs must be unique.")
    if len({winner.race_id for winner in ordered}) != len(ordered):
        raise Win5ResultInvariantError("Special result requires exactly one winner per Race.")
    return ordered


def fingerprint_special_result(
    winners: tuple[Win5SpecialResultWinner, ...],
) -> str:
    """Return the stable v1 fingerprint of one complete Special winner bundle."""

    ordered = _ordered_valid_result(tuple(winners))
    canonical = "win5-special-result-v1\n" + "\n".join(
        f"{winner.race_id}:{winner.id}:{winner.gate_number}" for winner in ordered
    )
    return sha256(canonical.encode("ascii")).hexdigest()
