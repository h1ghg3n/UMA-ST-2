"""Member-wide user-facing copy for bound WIN5 interactions."""

BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."


def authorization_error(reference_id: str) -> str:
    return f"권한을 확인하지 못했습니다. 참조 ID: `{reference_id}`"
