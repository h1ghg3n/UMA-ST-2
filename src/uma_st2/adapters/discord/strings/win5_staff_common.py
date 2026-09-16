"""Staff-wide WIN5 interaction copy and Round action registration."""

from __future__ import annotations

import discord

BOUND_CONTINUE = "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."
BOUND_MODAL_SUBMIT = "이 입력 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
ROUND_ACTION_PLACEHOLDER = "라운드 작업 선택"
ROUND_PANEL_PROMPT = "관리할 WIN5 라운드 작업을 선택해 주세요."
SEASON_PANEL_PROMPT = "관리할 WIN5 시즌 작업을 선택해 주세요."


def authorization_error(reference_id: str) -> str:
    return f"권한을 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def round_panel_error(reference_id: str) -> str:
    return f"작업 화면을 열지 못했습니다. 참조 ID: `{reference_id}`"


def season_panel_error(reference_id: str) -> str:
    return f"시즌 작업 화면을 열지 못했습니다. 참조 ID: `{reference_id}`"


def round_action_options(
    *,
    normal_create: str,
    special_create: str,
    delete: str,
    open_: str,
    close: str,
    normal_result_entry: str,
    normal_result_correction: str,
    special_result_entry: str,
    special_result_correction: str,
    special_void: str,
    special_round_cancel: str,
    normal_scoring: str,
    special_scoring: str,
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            value=normal_create,
            label="일반 라운드 생성",
            description="제목과 단일 Race를 setup 상태로 생성합니다.",
        ),
        discord.SelectOption(
            value=special_create,
            label="특별 라운드 생성",
            description="제목과 순서가 있는 Race 목록을 생성합니다.",
        ),
        discord.SelectOption(
            value=delete,
            label="setup 라운드 삭제",
            description="잘못 만든 Round와 모든 Race/Entry를 삭제합니다.",
        ),
        discord.SelectOption(
            value=open_,
            label="라운드 열기",
            description="준비된 Normal/Special Round의 제출을 시작합니다.",
        ),
        discord.SelectOption(
            value=close,
            label="라운드 마감",
            description="열린 Round의 제출을 동결하고 마감합니다.",
        ),
        discord.SelectOption(
            value=normal_result_entry,
            label="일반 결과 입력",
            description="닫힌 일반 라운드의 1~5착 결과를 입력합니다.",
        ),
        discord.SelectOption(
            value=normal_result_correction,
            label="일반 결과 정정",
            description="채점 전 일반 라운드의 전체 결과를 정정합니다.",
        ),
        discord.SelectOption(
            value=special_result_entry,
            label="특별 결과 입력",
            description="닫힌 특별 라운드의 Race별 우승 게이트를 입력합니다.",
        ),
        discord.SelectOption(
            value=special_result_correction,
            label="특별 결과 정정",
            description="채점 전 특별 라운드의 전체 결과를 정정합니다.",
        ),
        discord.SelectOption(
            value=special_void,
            label="특별 Race 취소·복원",
            description="공식 취소 사실을 사유와 함께 변경합니다.",
        ),
        discord.SelectOption(
            value=special_round_cancel,
            label="특별 라운드 전체 취소",
            description="남은 모든 Race를 void 처리하고 종료합니다.",
        ),
        discord.SelectOption(
            value=normal_scoring,
            label="일반 라운드 채점",
            description="닫힌 일반 라운드를 최종 채점하고 보상을 반영합니다.",
        ),
        discord.SelectOption(
            value=special_scoring,
            label="특별 라운드 채점",
            description="닫힌 특별 라운드를 최종 채점하고 승점을 반영합니다.",
        ),
    ]
