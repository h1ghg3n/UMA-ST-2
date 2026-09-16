"""User-facing copy for private staff Circle Point operations."""

from __future__ import annotations

from uma_st2.application.point import (
    AppliedStaffCirclePoint,
    StaffCirclePointChoice,
    StaffCirclePointOperation,
    StaffCirclePointPreview,
)
from uma_st2.domain.identity import PersonaStatus

from ..common import bounded_discord_message, safe_discord_text

GRANT_COMMAND_DESCRIPTION = "Persona에 서클 포인트를 지급합니다."
ADJUST_COMMAND_DESCRIPTION = "Persona의 서클 포인트를 signed delta로 조정합니다."
PERSONA_OPTION_DESCRIPTION = "서클 포인트를 변경할 Persona"
GRANT_MODAL_TITLE = "서클 포인트 지급"
ADJUST_MODAL_TITLE = "서클 포인트 조정"
GRANT_AMOUNT_LABEL = "지급량 (양의 정수)"
ADJUST_AMOUNT_LABEL = "조정량 (+/- 정수)"
REASON_LABEL = "사유 (필수)"
CONFIRM_LABEL = "지급 확정"
ADJUST_CONFIRM_LABEL = "조정 확정"
CANCEL_LABEL = "취소"
BOUND_INTERACTION_ERROR = "이 화면을 연 운영자와 서버·채널에서만 계속할 수 있습니다."
INVALID_INPUT = "금액과 사유를 확인해 주세요. 사유는 공백이 아닌 255자 이하여야 합니다."
TARGET_UNAVAILABLE = "선택한 Persona를 더 이상 찾을 수 없습니다. 대상을 다시 검색해 주세요."
WALLET_UNAVAILABLE = "선택한 Persona의 canonical Circle Point 지갑이 없어 변경하지 않았습니다."
INVALID_AMOUNT = "금액 또는 변경 후 잔액이 지원 범위를 벗어나 변경하지 않았습니다."
STALE_TARGET = "Persona 또는 잔액이 Preview 이후 변경되었습니다. 처음부터 다시 확인해 주세요."
IDEMPOTENCY_CONFLICT = "이 상호작용은 다른 처리에 이미 사용되었습니다. 처음부터 다시 시도해 주세요."
CONCURRENT_CONFLICT = "다른 포인트 작업과 동시에 처리되어 안전하게 취소했습니다. 잔액을 다시 확인해 주세요."
AUDIT_ERROR = "저장된 포인트 처리 증거가 완전하지 않아 변경하지 않았습니다. 운영 로그를 확인해 주세요."
CANCELLED = "서클 포인트 변경을 취소했습니다."
PANEL_TRANSITION_ERROR = (
    "서클 포인트 화면을 갱신하지 못했습니다. 처음 실행한 `/staff` 포인트 명령을 다시 실행해 주세요."
)

_STATUS_LABELS = {
    PersonaStatus.NORMAL: "정상",
    PersonaStatus.WARNING: "경고",
    PersonaStatus.PENDING_APPROVAL: "승인 대기",
    PersonaStatus.EXPELLED: "제명",
    PersonaStatus.WITHDRAWN: "탈퇴",
}


def autocomplete_label(choice: StaffCirclePointChoice) -> str:
    wallet = "wallet 있음" if choice.wallet_available else "wallet 없음"
    suffix = choice.persona_id[-8:]
    return safe_discord_text(
        f"{choice.display_name} · {_STATUS_LABELS[choice.status]} · {wallet} · {suffix}",
        limit=100,
    )


def format_preview(preview: StaffCirclePointPreview) -> str:
    operation = "지급" if preview.operation is StaffCirclePointOperation.GRANT else "운영 조정"
    return bounded_discord_message(
        (
            f"## 서클 포인트 {operation} Preview",
            f"Persona: {safe_discord_text(preview.state.display_name)}",
            f"상태: {_STATUS_LABELS[preview.state.status]}",
            f"현재 잔액: `{preview.state.balance:,}`",
            f"변경량: `{preview.amount:+,}`",
            f"변경 후 잔액: `{preview.resulting_balance:,}`",
            f"사유: {safe_discord_text(preview.reason, limit=255)}",
            "",
            "확정 시 현재 Persona와 wallet을 다시 확인하고 한 번의 transaction으로 반영합니다.",
        )
    )


def format_receipt(result: AppliedStaffCirclePoint) -> str:
    operation = "지급" if result.operation is StaffCirclePointOperation.GRANT else "운영 조정"
    retry = " · exact retry" if result.exact_retry else ""
    return bounded_discord_message(
        (
            f"## 서클 포인트 {operation} 완료",
            f"Persona: {safe_discord_text(result.display_name)}",
            f"변경량: `{result.amount:+,}`",
            f"현재 잔액: `{result.current_balance:,}`",
            f"거래: `#{result.transaction_id}`{retry}",
            f"사유: {safe_discord_text(result.reason, limit=255)}",
        )
    )
