"""Pure Special WIN5 Race-void state rules."""

from __future__ import annotations

from hashlib import sha256

from .errors import Win5ResultInvariantError


def _canonical_race_ids(
    race_ids: tuple[int, ...],
    *,
    allow_empty: bool,
    field_name: str,
) -> tuple[int, ...]:
    canonical = tuple(sorted(race_ids))
    if not allow_empty and not canonical:
        raise Win5ResultInvariantError(f"{field_name} must not be empty.")
    if any(isinstance(race_id, bool) or not isinstance(race_id, int) or race_id <= 0 for race_id in canonical):
        raise Win5ResultInvariantError(f"{field_name} must contain only positive integer Race IDs.")
    if len(set(canonical)) != len(canonical):
        raise Win5ResultInvariantError(f"{field_name} must contain unique Race IDs.")
    return canonical


def fingerprint_special_void_state(void_race_ids: tuple[int, ...]) -> str:
    """Return a stable fingerprint for the complete current Special void set."""

    canonical = _canonical_race_ids(
        tuple(void_race_ids),
        allow_empty=True,
        field_name="void_race_ids",
    )
    serialized = "win5-special-void-state-v1\n" + "\n".join(str(race_id) for race_id in canonical)
    return sha256(serialized.encode("ascii")).hexdigest()
