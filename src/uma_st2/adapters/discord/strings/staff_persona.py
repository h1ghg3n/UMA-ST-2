"""User-facing copy for the private Staff Persona workflow."""

from __future__ import annotations

from uma_st2.application.identity import (
    STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE,
    AddedGameAccount,
    ApprovedAccountRegistration,
    AttachedDiscordAccount,
    ChangedPersonaStatus,
    CorrectedGameAccountOwner,
    DirectlyRegisteredAccount,
    RejectedAccountRegistration,
    StaffDiscordAttachState,
    StaffDisplayGameAccountPage,
    StaffGameAccountAddPreview,
    StaffGameAccountDisplayPreview,
    StaffGameAccountOwnerCorrectionPreview,
    StaffPersonaDisplayPreview,
    StaffPersonaPanel,
    StaffPersonaStatusPreview,
    StaffPersonaStatusState,
    StaffRegistrationRequestPage,
    StaffRegistrationRequestState,
    UpdatedGameAccountDisplayInfo,
    UpdatedPersonaDisplayName,
)
from uma_st2.domain.identity import PersonaStatus

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

STAFF_ROOT_DESCRIPTION = "계정과 공통 운영 작업을 관리합니다."
PERSONA_COMMAND_DESCRIPTION = "Persona와 계정 등록 workflow를 관리합니다."
PERSONA_OPTION_DESCRIPTION = "선택할 기존 Persona (등록 요청 승인 대상이 아님)"
REVIEW_ACTION_LABEL = "등록 요청 검토"
ATTACH_ACTION_LABEL = "Discord 계정 연결"
ATTACH_MEMBER_PLACEHOLDER = "연결할 Discord 멤버를 선택하세요"
ATTACH_CONFIRM_LABEL = "연결 확정"
ATTACH_NOTE_LABEL = "메모 입력"
ATTACH_NOTE_EDIT_LABEL = "메모 수정"
ATTACH_RETARGET_LABEL = "대상 다시 선택"
ATTACH_NOTE_MODAL_TITLE = "Discord 계정 연결 메모"
ATTACH_NOTE_INPUT_LABEL = "운영 메모 (선택)"
DIRECT_REGISTER_ACTION_LABEL = "신규 직접 등록"
DIRECT_REGISTER_MEMBER_PLACEHOLDER = "직접 등록할 Discord 멤버를 선택하세요"
DIRECT_REGISTER_REGION_PLACEHOLDER = "게임 리전을 선택하세요"
DIRECT_REGISTER_INPUT_MODAL_TITLE = "신규 계정 직접 등록"
DIRECT_REGISTER_PID_LABEL = "PID"
DIRECT_REGISTER_NICKNAME_LABEL = "GameAccount 닉네임"
DIRECT_REGISTER_AFFILIATION_LABEL = "소속 (선택)"
DIRECT_REGISTER_NOTE_LABEL = "운영 메모 (선택)"
DIRECT_REGISTER_CONFIRM_LABEL = "신규 등록 확정"
DIRECT_REGISTER_REENTER_LABEL = "입력 수정"
DIRECT_REGISTER_RETARGET_LABEL = "대상 다시 선택"
GAME_ACCOUNT_ADD_ACTION_LABEL = "GameAccount 추가"
GAME_ACCOUNT_ADD_REGION_PLACEHOLDER = "추가할 계정의 게임 리전을 선택하세요"
GAME_ACCOUNT_ADD_INPUT_MODAL_TITLE = "Peer GameAccount 추가"
GAME_ACCOUNT_ADD_PID_LABEL = "PID"
GAME_ACCOUNT_ADD_NICKNAME_LABEL = "GameAccount 닉네임"
GAME_ACCOUNT_ADD_AFFILIATION_LABEL = "소속 (선택)"
GAME_ACCOUNT_ADD_REASON_LABEL = "추가 사유 (필수)"
GAME_ACCOUNT_ADD_CONFIRM_LABEL = "계정 추가 확정"
GAME_ACCOUNT_ADD_REENTER_LABEL = "입력 수정"
OWNER_CORRECTION_ACTION_LABEL = "GameAccount 소유자 정정"
OWNER_CORRECTION_REGION_PLACEHOLDER = "정정할 계정의 게임 리전을 선택하세요"
OWNER_CORRECTION_MODAL_TITLE = "GameAccount 소유자 정정"
OWNER_CORRECTION_PID_LABEL = "기존 PID"
OWNER_CORRECTION_REASON_LABEL = "검토 근거와 정정 사유 (필수)"
OWNER_CORRECTION_CONFIRM_LABEL = "소유자 정정 확정"
OWNER_CORRECTION_REENTER_LABEL = "입력 수정"
DISPLAY_EDIT_ACTION_LABEL = "표시 정보 수정"
DISPLAY_EDIT_PERSONA_ACTION_LABEL = "Persona 이름 수정"
DISPLAY_EDIT_GAME_ACCOUNT_ACTION_LABEL = "GameAccount 정보 수정"
DISPLAY_EDIT_PERSONA_MODAL_TITLE = "Persona 이름 수정"
DISPLAY_EDIT_PERSONA_NAME_LABEL = "Persona 표시명"
DISPLAY_EDIT_GAME_ACCOUNT_MODAL_TITLE = "GameAccount 정보 수정"
DISPLAY_EDIT_GAME_ACCOUNT_PLACEHOLDER = "수정할 GameAccount를 선택하세요"
DISPLAY_EDIT_NICKNAME_LABEL = "GameAccount 닉네임"
DISPLAY_EDIT_AFFILIATION_LABEL = "소속 (비우면 제거)"
DISPLAY_EDIT_REASON_LABEL = "수정 사유 (필수)"
DISPLAY_EDIT_PERSONA_CONFIRM_LABEL = "Persona 이름 수정 확정"
DISPLAY_EDIT_GAME_ACCOUNT_CONFIRM_LABEL = "계정 정보 수정 확정"
DISPLAY_EDIT_REENTER_LABEL = "입력 수정"
STATUS_ACTION_LABEL = "상태 관리"
STATUS_PLACEHOLDER = "변경할 Persona 상태를 선택하세요"
STATUS_REASON_MODAL_TITLE = "Persona 상태 변경"
STATUS_REASON_LABEL = "상태 변경 사유 (필수)"
STATUS_CONFIRM_LABEL = "상태 변경 확정"
STATUS_REENTER_LABEL = "사유 수정"
STATUS_RESELECT_LABEL = "상태 다시 선택"
BACK_LABEL = "뒤로"
CLOSE_LABEL = "닫기"
CLOSED_RECEIPT = "Staff Persona 화면을 닫았습니다."
PANEL_TRANSITION_ERROR = "계정 관리 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
APPROVE_LABEL = "승인"
APPROVE_CONFIRM_LABEL = "승인 확정"
REJECT_LABEL = "반려"
REJECTION_MODAL_TITLE = "계정 등록 요청 반려"
REJECTION_REASON_LABEL = "반려 사유"
BOUND_INTERACTION_ERROR = "이 화면을 연 운영자와 서버·채널에서만 계속할 수 있습니다."
REQUEST_UNAVAILABLE = "해당 등록 요청은 더 이상 검토할 수 없습니다. 목록을 새로 열어 확인해 주세요."
REQUEST_STALE = "등록 요청이 Preview 이후 변경되었습니다. 목록에서 다시 선택해 주세요."
REQUESTER_LINKED = "요청자가 이미 Persona에 연결되어 있어 자동 승인을 중단했습니다. 별도 연결 작업으로 확인해 주세요."
PID_UNAVAILABLE = "해당 리전/PID가 이미 등록되어 승인을 중단했습니다. 기존 계정을 확인해 주세요."
IDEMPOTENCY_CONFLICT = "이 상호작용은 다른 처리에 이미 사용되었습니다. Panel을 다시 열어 주세요."
CONCURRENT_CONFLICT = (
    "다른 등록 검토와 동시에 처리되어 이 승인은 안전하게 취소되었습니다. 목록을 다시 열어 확인해 주세요."
)
REVIEW_ALREADY_STARTED = "이 등록 요청 검토는 이미 처리 중이거나 완료되었습니다."
INVALID_INPUT = "입력값을 확인해 주세요. 반려 사유는 공백이 아닌 255자 이하여야 합니다."
ATTACH_PERSONA_UNAVAILABLE = "선택한 Persona를 더 이상 찾을 수 없습니다. Panel을 다시 열어 주세요."
ATTACH_TARGET_LINKED = "선택한 Discord 계정은 이미 Persona에 연결되어 있습니다. 자동 transfer하지 않았습니다."
ATTACH_PENDING_REQUEST = "선택한 Discord 계정에 pending 등록 요청이 있습니다. 등록 요청을 먼저 승인 또는 반려해 주세요."
ATTACH_STALE = "Persona 또는 계정 상태가 Preview 이후 변경되었습니다. Panel에서 다시 확인해 주세요."
ATTACH_CONCURRENT_CONFLICT = (
    "다른 계정 연결과 동시에 처리되어 이 작업은 안전하게 취소되었습니다. Panel을 다시 열어 주세요."
)
ATTACH_INVALID_INPUT = "Discord 계정 연결 입력을 확인해 주세요. 운영 메모는 255자 이하여야 합니다."
ATTACH_TRANSITION_ERROR = "Discord 계정 연결 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
DIRECT_REGISTER_TARGET_LINKED = "선택한 Discord 계정은 이미 Persona에 연결되어 있습니다. 자동 transfer하지 않았습니다."
DIRECT_REGISTER_PENDING_REQUEST = (
    "선택한 Discord 계정에 pending 등록 요청이 있습니다. 등록 요청을 먼저 승인 또는 반려해 주세요."
)
DIRECT_REGISTER_PID_UNAVAILABLE = "해당 리전/PID는 이미 등록되어 있습니다. 기존 GameAccount를 확인해 주세요."
DIRECT_REGISTER_STALE = (
    "Discord 계정, pending 요청 또는 PID 상태가 Preview 이후 변경되었습니다. 처음부터 다시 확인해 주세요."
)
DIRECT_REGISTER_CONCURRENT_CONFLICT = (
    "다른 계정 등록과 동시에 처리되어 이 작업은 안전하게 취소되었습니다. Panel을 다시 열어 주세요."
)
DIRECT_REGISTER_TRANSITION_ERROR = "직접 등록 화면을 갱신하지 못했습니다. `/staff persona`를 다시 열어 주세요."
DIRECT_REGISTER_INVALID_INPUT = "직접 등록 입력을 확인해 주세요. PID는 1~32자리 양의 정수이고 닉네임은 필수입니다."
GAME_ACCOUNT_ADD_PERSONA_UNAVAILABLE = "선택한 Persona를 더 이상 찾을 수 없습니다. Panel을 다시 열어 주세요."
GAME_ACCOUNT_ADD_PERSONA_RESTRICTED = "제명 또는 탈퇴 Persona에는 새 GameAccount를 추가할 수 없습니다."
GAME_ACCOUNT_ADD_PID_UNAVAILABLE = (
    "해당 리전/PID는 이미 등록되어 있습니다. 자동 이동하지 않았으며 필요하면 GameAccount 소유자 정정을 사용해 주세요."
)
GAME_ACCOUNT_ADD_STALE = "Persona 또는 GameAccount 상태가 Preview 이후 변경되었습니다. Panel에서 다시 확인해 주세요."
GAME_ACCOUNT_ADD_CONCURRENT_CONFLICT = (
    "다른 계정 작업과 동시에 처리되어 이 추가는 안전하게 취소되었습니다. Panel에서 다시 확인해 주세요."
)
GAME_ACCOUNT_ADD_INVALID_INPUT = (
    "GameAccount 입력을 확인해 주세요. PID는 1~32자리 양의 정수, 닉네임과 255자 이하 사유는 필수입니다."
)
OWNER_CORRECTION_UNAVAILABLE = (
    "선택한 Persona 또는 해당 리전/PID의 기존 GameAccount를 찾을 수 없습니다. Panel에서 다시 확인해 주세요."
)
OWNER_CORRECTION_SAME_OWNER = "해당 GameAccount는 이미 선택한 Persona의 소유라서 저장할 변경이 없습니다."
OWNER_CORRECTION_WALLET_MISSING = (
    "대상 Persona에 canonical Circle Point wallet이 없어 정정을 중단했습니다. "
    "이 작업은 wallet을 복구하거나 initial 500을 지급하지 않습니다."
)
OWNER_CORRECTION_STALE = (
    "Persona, account 소유 관계 또는 wallet 상태가 Preview 이후 변경되었습니다. 다시 확인해 주세요."
)
OWNER_CORRECTION_CONCURRENT_CONFLICT = (
    "다른 계정 작업과 동시에 처리되어 이 정정은 안전하게 취소되었습니다. Panel에서 다시 확인해 주세요."
)
OWNER_CORRECTION_INVALID_INPUT = (
    "소유자 정정 입력을 확인해 주세요. PID는 1~32자리 양의 정수이고 검토 근거와 사유는 필수입니다."
)
DISPLAY_EDIT_PERSONA_UNAVAILABLE = "선택한 Persona를 더 이상 찾을 수 없습니다. Panel을 다시 열어 주세요."
DISPLAY_EDIT_GAME_ACCOUNT_UNAVAILABLE = (
    "선택한 GameAccount가 없거나 더 이상 이 Persona 소유가 아닙니다. 목록을 다시 열어 주세요."
)
DISPLAY_EDIT_NO_CHANGE = "현재 값과 같아 저장할 변경이 없습니다."
DISPLAY_EDIT_STALE = "표시 정보 또는 소유 관계가 Preview 이후 변경되었습니다. Panel에서 다시 확인해 주세요."
DISPLAY_EDIT_CONCURRENT_CONFLICT = (
    "다른 계정 작업과 동시에 처리되어 이 수정은 안전하게 취소되었습니다. Panel에서 다시 확인해 주세요."
)
DISPLAY_EDIT_INVALID_INPUT = "표시 정보 입력을 확인해 주세요. 이름과 255자 이하 수정 사유는 필수입니다."
STATUS_PERSONA_UNAVAILABLE = "선택한 Persona를 더 이상 찾을 수 없습니다. Panel을 다시 열어 주세요."
STATUS_NO_CHANGE = "현재 상태와 같아 저장할 변경이 없습니다."
STATUS_STALE = "Persona 정보 또는 상태가 Preview 이후 변경되었습니다. Panel에서 다시 확인해 주세요."
STATUS_CONCURRENT_CONFLICT = (
    "다른 계정 작업과 동시에 처리되어 이 상태 변경은 안전하게 취소되었습니다. Panel에서 다시 확인해 주세요."
)
STATUS_INVALID_INPUT = "상태 변경 입력을 확인해 주세요. Canonical 상태와 255자 이하 사유가 필요합니다."

_PERSONA_STATUS_LABELS = {
    PersonaStatus.NORMAL: "정상",
    PersonaStatus.WARNING: "경고",
    PersonaStatus.PENDING_APPROVAL: "승인 대기",
    PersonaStatus.EXPELLED: "제명",
    PersonaStatus.WITHDRAWN: "탈퇴",
}


def persona_status_label(status: PersonaStatus) -> str:
    return _PERSONA_STATUS_LABELS[PersonaStatus(status)]


def _status_consequence(status: PersonaStatus) -> list[str]:
    if status is PersonaStatus.NORMAL:
        return [
            "- 새 member mutation status gate: 허용",
            "- 실제 사용에는 wallet과 PID 등록 GameAccount가 계속 필요합니다.",
        ]
    if status is PersonaStatus.WARNING:
        return [
            "- 새 member mutation status gate: 경고 표시와 함께 허용",
            "- 실제 사용에는 wallet과 PID 등록 GameAccount가 계속 필요합니다.",
        ]
    if status is PersonaStatus.PENDING_APPROVAL:
        return [
            "- 새 Match Bet/WIN5 mutation: 승인 대기 안내로 차단",
            "- 기존 status/history read와 accepted fact의 후속 처리는 유지합니다.",
        ]
    return [
        "- 새 member mutation: 차단",
        "- 기존 history와 accepted fact의 후속 처리는 유지합니다.",
    ]


def mask_pid(pid: str) -> str:
    return f"••••{pid[-4:]}" if len(pid) > 4 else "••••"


def format_staff_persona_panel(panel: StaffPersonaPanel) -> str:
    lines = [
        "## Staff Persona",
        f"- 검토 대기 등록 요청: {panel.pending_request_count}건",
    ]
    if panel.selected_persona is None:
        lines.append("- 선택 Persona: 없음")
    else:
        selected = panel.selected_persona
        lines.extend(
            [
                f"- 선택 Persona: {safe_discord_text(selected.display_name, limit=100)}",
                f"- 상태: {_PERSONA_STATUS_LABELS[selected.status]} · GameAccount {selected.game_account_count}개",
            ]
        )
    lines.extend(
        [
            "",
            "등록 요청 검토, 신규 직접 등록과 selected Persona의 "
            "Discord access·peer GameAccount 관리 기능을 제공합니다.",
            "선택 Persona는 panel context이며 등록 요청 승인 대상이 아닙니다.",
        ]
    )
    return bounded_discord_message(lines, limit=3500)


def format_registration_request_page(page: StaffRegistrationRequestPage) -> str:
    total_pages = max(1, (page.total_count + 9) // 10)
    lines = [f"## 계정 등록 요청 · {page.page + 1}/{total_pages}"]
    if not page.items:
        lines.append("검토할 pending 요청이 없습니다.")
        return bounded_discord_message(lines, limit=3500)
    for item in page.items:
        lines.extend(
            [
                "",
                f"### 요청 #{item.request_id} · {safe_discord_text(item.discord_display_name_snapshot, limit=80)}",
                (f"{item.game_region.value} · {mask_pid(item.uma_pid)} · {safe_discord_text(item.nickname, limit=80)}"),
                f"접수 {format_discord_datetime(item.created_at)}",
            ]
        )
    return bounded_discord_message(lines, limit=3500)


def format_registration_request_detail(request: StaffRegistrationRequestState) -> str:
    return bounded_discord_message(
        [
            "## 계정 등록 요청 Preview",
            f"- 요청 ID: `{request.request_id}`",
            f"- Discord 표시명 snapshot: {safe_discord_text(request.discord_display_name_snapshot, limit=100)}",
            f"- Discord user ID: `{request.requester_discord_user_id}`",
            f"- 리전/PID: {request.game_region.value} · `{request.uma_pid}`",
            f"- GameAccount 닉네임: {safe_discord_text(request.nickname, limit=100)}",
            f"- 소속: {safe_discord_text(request.affiliation, limit=100) if request.affiliation else '없음'}",
            f"- 접수: {format_discord_datetime(request.created_at)}",
            "",
            "승인은 이 snapshot으로 fresh Persona, GameAccount와 초기 Circle Point를 만듭니다.",
            "기존 Persona를 선택했더라도 이 요청과 자동 병합하지 않습니다.",
        ],
        limit=3500,
    )


def format_registration_approval_confirmation(request: StaffRegistrationRequestState) -> str:
    return bounded_discord_message(
        [
            "## 계정 등록 승인 최종 확인",
            f"- 요청 ID: `{request.request_id}`",
            f"- Persona 이름: {safe_discord_text(request.discord_display_name_snapshot, limit=100)}",
            (
                f"- GameAccount: {request.game_region.value} · {mask_pid(request.uma_pid)} · "
                f"{safe_discord_text(request.nickname, limit=100)}"
            ),
            "- Persona 상태: normal",
            "- 새 wallet: 0 → initial_grant +500",
            "",
            "Final Confirm에서 current request, requester link와 PID uniqueness를 다시 확인합니다.",
        ],
        limit=3500,
    )


def format_registration_approval_receipt(result: ApprovedAccountRegistration) -> str:
    lines = [
        "## 계정 등록 승인 완료",
        f"- 요청 ID: `{result.request_id}`",
        f"- Persona: {safe_discord_text(result.persona_display_name, limit=100)} · normal",
        (
            f"- GameAccount: {result.game_region.value} · {mask_pid(result.uma_pid)} · "
            f"{safe_discord_text(result.nickname, limit=100)}"
        ),
        f"- Circle Point: +{result.initial_grant_amount} · 현재 {result.wallet_balance}",
        f"- 처리 시각: {format_discord_datetime(result.resolved_at)}",
    ]
    if result.exact_retry:
        lines.append("동일한 승인 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_registration_rejection_receipt(result: RejectedAccountRegistration) -> str:
    lines = [
        "## 계정 등록 요청 반려 완료",
        f"- 요청 ID: `{result.request_id}`",
        "- 상태: cancelled",
        f"- 사유: {safe_discord_text(result.reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.resolved_at)}",
        "",
        "Persona, GameAccount, wallet과 Circle Point는 생성하지 않았습니다.",
    ]
    if result.exact_retry:
        lines.append("동일한 반려 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_discord_attach_picker(*, persona_id: str, persona_display_name: str) -> str:
    return bounded_discord_message(
        [
            "## Discord 계정 연결",
            f"- 대상 Persona: {safe_discord_text(persona_display_name, limit=100)} · `{persona_id[-8:]}`",
            "",
            "연결할 Discord 멤버를 선택해 주세요.",
            "이 작업은 Persona access만 연결하며 GameAccount, wallet, Circle Point와 "
            "과거 Match 귀속을 바꾸지 않습니다.",
        ],
        limit=3500,
    )


def format_discord_attach_preview(
    preview: StaffDiscordAttachState,
    *,
    target_display_name: str,
    operational_note: str | None,
) -> str:
    eligibility = "사용 가능" if preview.member_mutation_eligible else "추가 요건 필요"
    return bounded_discord_message(
        [
            "## Discord 계정 연결 Preview",
            f"- Discord 멤버: {safe_discord_text(target_display_name, limit=100)} · `{preview.target_discord_user_id}`",
            f"- 대상 Persona: {safe_discord_text(preview.persona_display_name, limit=100)}",
            f"- Persona 상태: {_PERSONA_STATUS_LABELS[preview.persona_status]}",
            f"- Wallet: {'준비됨' if preview.has_wallet else '없음'}",
            f"- PID 등록 GameAccount: {preview.qualifying_game_account_count}개",
            f"- 연결 후 member mutation: {eligibility}",
            f"- 운영 메모: {safe_discord_text(operational_note, limit=255) if operational_note else '없음'}",
            "",
            "Final Confirm에서 current link, active 등록 요청과 Persona 상태를 다시 확인합니다.",
            "Existing link는 덮어쓰거나 transfer하지 않습니다.",
        ],
        limit=3500,
    )


def format_discord_attach_receipt(result: AttachedDiscordAccount) -> str:
    eligibility = "사용 가능" if result.member_mutation_eligible else "추가 요건 필요"
    lines = [
        "## Discord 계정 연결 완료",
        f"- Discord user ID: `{result.target_discord_user_id}`",
        f"- Persona: {safe_discord_text(result.persona_display_name, limit=100)}",
        f"- Persona 상태: {_PERSONA_STATUS_LABELS[result.persona_status]}",
        f"- Wallet: {'준비됨' if result.has_wallet else '없음'}",
        f"- PID 등록 GameAccount: {result.qualifying_game_account_count}개",
        f"- Member mutation: {eligibility}",
        f"- 연결 시각: {format_discord_datetime(result.attached_at)}",
        "",
        "Discord access만 연결했으며 GameAccount, wallet/Circle Point와 과거 Match 귀속은 변경하지 않았습니다.",
    ]
    if result.operational_note:
        lines.insert(7, f"- 운영 메모: {safe_discord_text(result.operational_note, limit=255)}")
    if result.exact_retry:
        lines.append("동일한 연결 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_direct_registration_picker() -> str:
    return bounded_discord_message(
        [
            "## 신규 직접 등록",
            "직접 등록할 Discord 멤버를 선택해 주세요.",
            "기존 Persona를 선택했더라도 새 Persona를 만드는 별도 workflow입니다.",
        ],
        limit=3500,
    )


def format_direct_registration_region(*, target_display_name: str, target_discord_user_id: str) -> str:
    return bounded_discord_message(
        [
            "## 신규 직접 등록 · 리전 선택",
            f"- Discord 멤버: {safe_discord_text(target_display_name, limit=100)} · `{target_discord_user_id}`",
            "",
            "등록할 GameAccount의 리전을 선택해 주세요.",
        ],
        limit=3500,
    )


def format_direct_registration_preview(
    *,
    target_display_name: str,
    target_discord_user_id: str,
    game_region: str,
    uma_pid: str,
    nickname: str,
    affiliation: str | None,
    operational_note: str | None,
) -> str:
    return bounded_discord_message(
        [
            "## 신규 직접 등록 Preview",
            f"- Discord 멤버: {safe_discord_text(target_display_name, limit=100)} · `{target_discord_user_id}`",
            f"- 새 Persona 이름: {safe_discord_text(target_display_name, limit=100)}",
            "- Persona 상태: normal",
            f"- GameAccount: {game_region} · `{uma_pid}` · {safe_discord_text(nickname, limit=100)}",
            f"- 소속: {safe_discord_text(affiliation, limit=100) if affiliation else '없음'}",
            "- Circle Point: wallet 0 생성 → initial_grant +500",
            f"- 운영 메모: {safe_discord_text(operational_note, limit=255) if operational_note else '없음'}",
            "",
            "Final Confirm에서 existing link, active 등록 요청과 PID uniqueness를 다시 확인합니다.",
            "선택된 기존 Persona에 합치거나 pending 요청을 암묵적으로 처리하지 않습니다.",
        ],
        limit=3500,
    )


def format_direct_registration_receipt(result: DirectlyRegisteredAccount) -> str:
    lines = [
        "## 신규 직접 등록 완료",
        f"- Discord user ID: `{result.target_discord_user_id}`",
        f"- Persona: {safe_discord_text(result.persona_display_name, limit=100)} · normal",
        (
            f"- GameAccount: {result.game_region.value} · {mask_pid(result.uma_pid)} · "
            f"{safe_discord_text(result.nickname, limit=100)}"
        ),
        f"- 소속: {safe_discord_text(result.affiliation, limit=100) if result.affiliation else '없음'}",
        f"- Circle Point: +{result.initial_grant_amount} · 현재 {result.wallet_balance}",
        f"- 처리 시각: {format_discord_datetime(result.registered_at)}",
    ]
    if result.operational_note:
        lines.insert(6, f"- 운영 메모: {safe_discord_text(result.operational_note, limit=255)}")
    if result.exact_retry:
        lines.append("동일한 직접 등록 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_game_account_add_region(*, persona_display_name: str) -> str:
    return bounded_discord_message(
        [
            "## Peer GameAccount 추가 · 리전 선택",
            f"- 대상 Persona: {safe_discord_text(persona_display_name, limit=100)}",
            "",
            "새 GameAccount의 리전을 선택해 주세요.",
            "기존 PID의 소유권은 이 workflow에서 이동하지 않습니다.",
        ],
        limit=3500,
    )


def format_game_account_add_preview(preview: StaffGameAccountAddPreview) -> str:
    state = preview.state
    eligibility = "사용 가능" if state.resulting_member_mutation_eligible else "추가 요건 필요"
    return bounded_discord_message(
        [
            "## Peer GameAccount 추가 Preview",
            f"- 대상 Persona: {safe_discord_text(state.persona_display_name, limit=100)}",
            f"- Persona 상태: {_PERSONA_STATUS_LABELS[state.persona_status]}",
            f"- 새 GameAccount: {state.game_region.value} · `{state.uma_pid}` · "
            f"{safe_discord_text(preview.nickname, limit=100)}",
            f"- 소속: {safe_discord_text(preview.affiliation, limit=100) if preview.affiliation else '없음'}",
            f"- 추가 후 account 수: {state.game_account_count + 1}개",
            f"- Wallet: {'준비됨' if state.has_wallet else '없음'}",
            f"- 추가 후 member mutation: {eligibility}",
            f"- 사유: {safe_discord_text(preview.reason, limit=255)}",
            "",
            "Final Confirm에서 current Persona 상태와 PID ownership을 다시 확인합니다.",
            "Wallet, Circle Point, 기존 Rating과 Match 귀속은 변경하지 않습니다.",
        ],
        limit=3500,
    )


def format_game_account_add_receipt(result: AddedGameAccount) -> str:
    eligibility = "사용 가능" if result.member_mutation_eligible else "추가 요건 필요"
    lines = [
        "## Peer GameAccount 추가 완료",
        f"- Persona: {safe_discord_text(result.persona_display_name, limit=100)}",
        f"- Persona 상태: {_PERSONA_STATUS_LABELS[result.persona_status]}",
        (
            f"- GameAccount: {result.game_region.value} · {mask_pid(result.uma_pid)} · "
            f"{safe_discord_text(result.nickname, limit=100)}"
        ),
        f"- 소속: {safe_discord_text(result.affiliation, limit=100) if result.affiliation else '없음'}",
        f"- Account 수: {result.game_account_count}개 · PID 등록 {result.qualifying_game_account_count}개",
        f"- Member mutation: {eligibility}",
        f"- 사유: {safe_discord_text(result.reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.added_at)}",
        "",
        "GameAccount와 audit만 저장했으며 wallet, Circle Point, Rating과 Match 귀속은 변경하지 않았습니다.",
    ]
    if result.exact_retry:
        lines.append("동일한 계정 추가 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_owner_correction_region(*, persona_display_name: str) -> str:
    return bounded_discord_message(
        [
            "## GameAccount 소유자 정정 · 리전 선택",
            f"- 새 소유 Persona: {safe_discord_text(persona_display_name, limit=100)}",
            "",
            "현재 다른 Persona가 소유한 기존 PID의 리전을 선택해 주세요.",
            "이 작업은 검토된 V2 canonical 소유 오류를 정정할 때만 사용합니다.",
        ],
        limit=3500,
    )


def format_owner_correction_preview(preview: StaffGameAccountOwnerCorrectionPreview) -> str:
    state = preview.state
    source_eligibility = "사용 가능" if state.source_resulting_member_mutation_eligible else "추가 요건 필요"
    target_eligibility = "사용 가능" if state.target_resulting_member_mutation_eligible else "추가 요건 필요"
    return bounded_discord_message(
        [
            "## GameAccount 소유자 정정 Preview",
            (f"- 계정: {state.game_region.value} · `{state.uma_pid}` · {safe_discord_text(state.nickname, limit=100)}"),
            f"- 소속: {safe_discord_text(state.affiliation, limit=100) if state.affiliation else '없음'}",
            "",
            f"### 현재 소유자 · {safe_discord_text(state.source_persona_display_name, limit=100)}",
            f"- 상태: {_PERSONA_STATUS_LABELS[state.source_persona_status]}",
            f"- Wallet: {'준비됨' if state.source_has_wallet else '없음'}",
            (
                f"- Account: {state.source_game_account_count} → {state.source_resulting_game_account_count}개 · "
                f"PID 등록 {state.source_qualifying_game_account_count} → "
                f"{state.source_resulting_qualifying_game_account_count}개"
            ),
            f"- 정정 후 member mutation: {source_eligibility}",
            "",
            f"### 새 소유자 · {safe_discord_text(state.target_persona_display_name, limit=100)}",
            f"- 상태: {_PERSONA_STATUS_LABELS[state.target_persona_status]} (변경 없음)",
            f"- Wallet: {'준비됨' if state.target_has_wallet else '없음'}",
            (
                f"- Account: {state.target_game_account_count} → {state.target_resulting_game_account_count}개 · "
                f"PID 등록 {state.target_qualifying_game_account_count} → "
                f"{state.target_resulting_qualifying_game_account_count}개"
            ),
            f"- 정정 후 member mutation: {target_eligibility}",
            f"- 검토 근거/사유: {safe_discord_text(preview.evidence_reason, limit=255)}",
            "",
            "Final Confirm에서 두 Persona, exact PID, 소유 관계와 target wallet을 다시 확인합니다.",
            "현재 owner만 이동하며 wallet/Circle Point, status, Rating, "
            "과거 Match snapshot과 WIN5 fact는 바꾸지 않습니다.",
        ],
        limit=3500,
    )


def format_owner_correction_receipt(result: CorrectedGameAccountOwner) -> str:
    source_eligibility = "사용 가능" if result.source_member_mutation_eligible else "추가 요건 필요"
    target_eligibility = "사용 가능" if result.target_member_mutation_eligible else "추가 요건 필요"
    lines = [
        "## GameAccount 소유자 정정 완료",
        (
            f"- 계정: {result.game_region.value} · {mask_pid(result.uma_pid)} · "
            f"{safe_discord_text(result.nickname, limit=100)}"
        ),
        (
            f"- 소유자: {safe_discord_text(result.source_persona_display_name, limit=100)} → "
            f"{safe_discord_text(result.target_persona_display_name, limit=100)}"
        ),
        (f"- 현재 소유자 account: {result.source_game_account_count}개 · member mutation {source_eligibility}"),
        (f"- 새 소유자 account: {result.target_game_account_count}개 · member mutation {target_eligibility}"),
        f"- 검토 근거/사유: {safe_discord_text(result.evidence_reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.corrected_at)}",
        "",
        "GameAccount current owner와 audit만 저장했으며 wallet/Circle Point, Rating과 과거 귀속은 변경하지 않았습니다.",
    ]
    if result.exact_retry:
        lines.append("동일한 정정 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_persona_status_management(state: StaffPersonaStatusState) -> str:
    return bounded_discord_message(
        [
            "## Persona 상태 관리",
            f"- 대상 Persona: {safe_discord_text(state.display_name, limit=100)}",
            f"- 현재 상태: {persona_status_label(state.status)} (`{state.status.value}`)",
            "",
            "변경할 상태를 선택해 주세요. 모든 변경은 사유와 Final Confirm을 요구합니다.",
            "상태 변경은 계정 연결, wallet, Circle Point와 과거 기록을 변경하지 않습니다.",
        ],
        limit=3500,
    )


def format_persona_status_preview(preview: StaffPersonaStatusPreview) -> str:
    return bounded_discord_message(
        [
            "## Persona 상태 변경 Preview",
            f"- 대상 Persona: {safe_discord_text(preview.state.display_name, limit=100)}",
            f"- 변경 전: {persona_status_label(preview.state.status)} (`{preview.state.status.value}`)",
            f"- 변경 후: {persona_status_label(preview.desired_status)} (`{preview.desired_status.value}`)",
            f"- 사유: {safe_discord_text(preview.reason, limit=255)}",
            "",
            *_status_consequence(preview.desired_status),
            "",
            "Final Confirm에서 current Persona 정보와 상태를 다시 확인합니다.",
            "Discord/GameAccount 소유, Circle Point, Rating과 Match/WIN5 history는 바뀌지 않습니다.",
        ],
        limit=3500,
    )


def format_persona_status_receipt(result: ChangedPersonaStatus) -> str:
    lines = [
        "## Persona 상태 변경 완료",
        f"- Persona: {safe_discord_text(result.display_name, limit=100)}",
        f"- 변경: {persona_status_label(result.previous_status)} → {persona_status_label(result.status)}",
        f"- 사유: {safe_discord_text(result.reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.updated_at)}",
        "",
        *_status_consequence(result.status),
        "",
        "상태와 audit만 저장했으며 계정 연결, wallet/Circle Point와 과거 기록은 변경하지 않았습니다.",
    ]
    if result.exact_retry:
        lines.append("동일한 상태 변경 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_display_edit_scope(*, persona_display_name: str) -> str:
    return bounded_discord_message(
        [
            "## 표시 정보 수정",
            f"- 대상 Persona: {safe_discord_text(persona_display_name, limit=100)}",
            "",
            "수정할 범위를 선택해 주세요.",
            "이 workflow는 Persona 상태, PID, 계정 소유권과 과거 경기 snapshot을 변경하지 않습니다.",
        ],
        limit=3500,
    )


def format_display_game_account_page(page: StaffDisplayGameAccountPage) -> str:
    start = page.page * STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE + 1 if page.items else 0
    end = start + len(page.items) - 1 if page.items else 0
    return bounded_discord_message(
        [
            "## GameAccount 정보 수정 · 계정 선택",
            f"- 대상 Persona: {safe_discord_text(page.persona.display_name, limit=100)}",
            f"- 계정: {start}~{end} / {page.total_count}",
            "",
            "닉네임과 현재 소속만 수정할 수 있습니다. Region/PID와 owner는 변경되지 않습니다.",
        ],
        limit=3500,
    )


def format_persona_display_preview(preview: StaffPersonaDisplayPreview) -> str:
    return bounded_discord_message(
        [
            "## Persona 이름 수정 Preview",
            f"- 상태: {_PERSONA_STATUS_LABELS[preview.state.status]}",
            f"- 변경 전: {safe_discord_text(preview.state.display_name, limit=100)}",
            f"- 변경 후: {safe_discord_text(preview.display_name, limit=100)}",
            f"- 사유: {safe_discord_text(preview.reason, limit=255)}",
            "",
            "Final Confirm에서 current Persona 표시명과 상태를 다시 확인합니다.",
            "Discord nickname, GameAccount, access와 Circle Point는 변경하지 않습니다.",
        ],
        limit=3500,
    )


def format_game_account_display_preview(preview: StaffGameAccountDisplayPreview) -> str:
    state = preview.state
    pid = "PID 없음" if state.uma_pid is None else mask_pid(state.uma_pid)
    return bounded_discord_message(
        [
            "## GameAccount 정보 수정 Preview",
            f"- 대상 Persona: {safe_discord_text(state.persona_display_name, limit=100)}",
            f"- 계정: {state.game_region.value} · {pid}",
            f"- 닉네임: {safe_discord_text(state.nickname, limit=100)}"
            f" → {safe_discord_text(preview.nickname, limit=100)}",
            "- 소속: "
            f"{safe_discord_text(state.affiliation, limit=100) if state.affiliation else '없음'}"
            f" → {safe_discord_text(preview.affiliation, limit=100) if preview.affiliation else '없음'}",
            f"- 사유: {safe_discord_text(preview.reason, limit=255)}",
            "",
            "Final Confirm에서 Persona 소유 관계와 current account 정보를 다시 확인합니다.",
            "Region/PID, Rating과 과거 Match snapshot은 변경하지 않습니다.",
        ],
        limit=3500,
    )


def format_persona_display_receipt(result: UpdatedPersonaDisplayName) -> str:
    lines = [
        "## Persona 이름 수정 완료",
        f"- 변경 전: {safe_discord_text(result.previous_display_name, limit=100)}",
        f"- 변경 후: {safe_discord_text(result.display_name, limit=100)}",
        f"- 상태: {_PERSONA_STATUS_LABELS[result.status]}",
        f"- 사유: {safe_discord_text(result.reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.updated_at)}",
    ]
    if result.exact_retry:
        lines.append("동일한 표시명 수정 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_game_account_display_receipt(result: UpdatedGameAccountDisplayInfo) -> str:
    pid = "PID 없음" if result.uma_pid is None else mask_pid(result.uma_pid)
    lines = [
        "## GameAccount 정보 수정 완료",
        f"- Persona: {safe_discord_text(result.persona_display_name, limit=100)}",
        f"- 계정: {result.game_region.value} · {pid}",
        f"- 닉네임: {safe_discord_text(result.previous_nickname, limit=100)}"
        f" → {safe_discord_text(result.nickname, limit=100)}",
        "- 소속: "
        f"{safe_discord_text(result.previous_affiliation, limit=100) if result.previous_affiliation else '없음'}"
        f" → {safe_discord_text(result.affiliation, limit=100) if result.affiliation else '없음'}",
        f"- 사유: {safe_discord_text(result.reason, limit=255)}",
        f"- 처리 시각: {format_discord_datetime(result.updated_at)}",
    ]
    if result.exact_retry:
        lines.append("동일한 계정 정보 수정 결과가 이미 저장되어 기존 receipt를 표시했습니다.")
    return bounded_discord_message(lines, limit=3500)


def internal_error(reference_id: str) -> str:
    return f"계정 등록 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def attach_internal_error(reference_id: str) -> str:
    return f"Discord 계정 연결을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def direct_registration_internal_error(reference_id: str) -> str:
    return f"직접 등록을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def game_account_add_internal_error(reference_id: str) -> str:
    return f"GameAccount 추가를 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def owner_correction_internal_error(reference_id: str) -> str:
    return f"GameAccount 소유자 정정을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def display_edit_internal_error(reference_id: str) -> str:
    return f"표시 정보 수정을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"
