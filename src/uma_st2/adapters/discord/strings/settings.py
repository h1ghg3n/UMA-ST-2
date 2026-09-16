"""User-facing copy for the private Discord settings panel."""

from __future__ import annotations

from uma_st2.application.discord import (
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdatePreview,
    DiscordGuildSettingsUpdateState,
    UpdatedDiscordGuildSettings,
)

from ..common import safe_discord_text

COMMAND_DESCRIPTION = "서버 운영 설정을 확인하고 변경합니다"
TITLE = "## 서버 운영 설정"
PREVIEW_TITLE = "## 서버 운영 설정 변경 검토"
SUCCESS_TITLE = "## 서버 운영 설정 변경 완료"
GUIDANCE = "화면 편집은 임시 초안입니다. 검토 후 최종 확정할 때만 DB에 반영됩니다."
CHANNEL_GUIDANCE = "기존 text/announcement channel 하나를 선택하거나 설정을 해제해 주세요."
BOUND_INTERACTION_ERROR = "이 화면을 연 운영자와 서버·채널에서만 계속할 수 있습니다."
UNAVAILABLE = "현재 서버 설정을 불러오거나 변경할 수 없습니다."
STALE = "설정이 다른 작업에서 변경되었습니다. `/settings`를 다시 열어 주세요."
NO_CHANGE = "변경된 설정이 없습니다. DB에는 기록되지 않았습니다."
INVALID_INPUT = "입력한 설정 값이 유효하지 않습니다."
INVALID_CHANNEL = "선택한 채널이 현재 목적지 권한 조건을 충족하지 않습니다."
IDEMPOTENCY_CONFLICT = "같은 최종 요청 키가 다른 설정 변경에 이미 사용되었습니다."
CONCURRENT_CONFLICT = "동시 설정 변경과 충돌했습니다. `/settings`를 다시 열어 주세요."
INTERNAL_ERROR = "설정 변경을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
CLOSED = "설정 화면을 닫았습니다."
AUTHORIZATION_LOST = "현재 설정 권한을 확인할 수 없어 화면을 종료했습니다."
EXPIRED_VIEW = "이미 종료되거나 교체된 설정 화면입니다. `/settings`를 다시 열어 주세요."
REOPEN_SETTINGS = "설정 화면을 갱신하지 못했습니다. `/settings`를 다시 열어 주세요."

WIN5_CHANNEL_LABEL = "WIN5 공지 채널"
MATCH_CHANNEL_LABEL = "Match 공지 채널"
LOG_CHANNEL_LABEL = "로그 채널"
TIMEZONE_LABEL = "시간대 설정"
WIN5_ENABLED_LABEL = "WIN5 공지"
MATCH_ENABLED_LABEL = "Match 공지"
REVIEW_LABEL = "변경 검토"
CANCEL_LABEL = "닫기"
CLEAR_LABEL = "채널 설정 해제"
BACK_LABEL = "설정으로 돌아가기"
CONFIRM_LABEL = "변경 확정"
EDIT_LABEL = "다시 수정"
REASON_LABEL = "변경 사유"
REASON_MODAL_TITLE = "설정 변경 검토"
TIMEZONE_MODAL_TITLE = "시간대 설정 변경"

FIELD_LABELS = {
    "win5_announcement_channel_id": WIN5_CHANNEL_LABEL,
    "match_announcement_channel_id": MATCH_CHANNEL_LABEL,
    "log_channel_id": LOG_CHANNEL_LABEL,
    "default_timezone": TIMEZONE_LABEL,
    "win5_announcements_enabled": WIN5_ENABLED_LABEL,
    "match_announcements_enabled": MATCH_ENABLED_LABEL,
}


def _channel(channel_id: str | None) -> str:
    return "설정 안 됨" if channel_id is None else f"<#{channel_id}>"


def _enabled(value: bool) -> str:
    return "사용" if value else "중지"


def _value(settings: DiscordGuildEditableSettings, field_name: str) -> str:
    value = getattr(settings, field_name)
    if field_name.endswith("channel_id"):
        return _channel(value)
    if field_name.endswith("enabled"):
        return _enabled(value)
    return f"`{safe_discord_text(str(value), limit=64)}`"


def format_panel(
    state: DiscordGuildSettingsUpdateState,
    desired: DiscordGuildEditableSettings,
) -> str:
    changed = state.editable.changed_fields(desired)
    roles = ", ".join(
        f"<@&{role_id}>" for role_id in (state.operator_role_id, state.bot_manager_role_id) if role_id is not None
    )
    return "\n".join(
        (
            TITLE,
            f"WIN5 공지 채널: {_channel(desired.win5_announcement_channel_id)}",
            f"Match 공지 채널: {_channel(desired.match_announcement_channel_id)}",
            f"로그 채널: {_channel(desired.log_channel_id)}",
            f"시간대 설정: `{safe_discord_text(desired.default_timezone, limit=32)}`",
            f"WIN5 공지: **{_enabled(desired.win5_announcements_enabled)}**",
            f"Match 공지: **{_enabled(desired.match_announcements_enabled)}**",
            f"운영 Role (읽기 전용): {roles}",
            f"초안 변경 항목: **{len(changed)}개**",
            GUIDANCE,
        )
    )


def format_channel_editor(label: str, channel_id: str | None) -> str:
    return "\n".join(
        (
            f"## {label}",
            f"현재 초안: {_channel(channel_id)}",
            CHANNEL_GUIDANCE,
        )
    )


def format_preview(preview: DiscordGuildSettingsUpdatePreview) -> str:
    lines = [PREVIEW_TITLE]
    for field_name in preview.changed_fields:
        lines.append(
            f"- {FIELD_LABELS[field_name]}: {_value(preview.before.editable, field_name)} → "
            f"{_value(preview.desired, field_name)}"
        )
    lines.extend(
        (
            f"변경 사유: {safe_discord_text(preview.reason, limit=255)}",
            "최종 확정 시 현재 설정을 다시 잠그고 검증합니다.",
        )
    )
    return "\n".join(lines)


def format_success(result: UpdatedDiscordGuildSettings) -> str:
    fields = ", ".join(FIELD_LABELS[field_name] for field_name in result.changed_fields)
    retry = " (동일 요청 재확인)" if result.exact_retry else ""
    return "\n".join(
        (
            f"{SUCCESS_TITLE}{retry}",
            f"변경 항목: {fields}",
            f"변경 사유: {safe_discord_text(result.reason, limit=255)}",
            f"Operation: `{result.operation_id}`",
        )
    )
