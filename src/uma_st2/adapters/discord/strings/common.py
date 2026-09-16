"""Feature-independent Discord response copy."""

from __future__ import annotations


def omitted_items_suffix(count: int) -> str:
    """Render the existing bounded-message omission suffix."""

    return f"… {count}개 항목 생략"


def internal_error_message(request_id: str) -> str:
    """Render the existing private generic failure response."""

    return f"요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{request_id}`"
