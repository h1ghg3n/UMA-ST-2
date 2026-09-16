"""Persistence invariant for the nullable singleton WIN5 Season marker."""

from __future__ import annotations

from uma_st2.domain.win5 import Win5SeasonStatus


def win5_season_active_marker(status: Win5SeasonStatus | str) -> bool | None:
    """Return the only valid marker for one canonical Season status."""

    canonical_status = Win5SeasonStatus(status)
    return True if canonical_status == Win5SeasonStatus.ACTIVE else None


def validate_win5_season_marker(
    *,
    status: Win5SeasonStatus | str,
    active_marker: bool | None,
) -> Win5SeasonStatus:
    """Fail closed when persisted status and marker describe different states."""

    canonical_status = Win5SeasonStatus(status)
    expected_marker = win5_season_active_marker(canonical_status)
    if active_marker is not expected_marker:
        raise ValueError("WIN5 Season status and active_marker are inconsistent.")
    return canonical_status
