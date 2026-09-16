"""User-facing registration copy for WIN5 commands."""

from ..localization import korean_parameter_name

ROUND_ID_OPTION_NAME = korean_parameter_name("round_id", "회차")
SUBMISSION_ID_OPTION_NAME = korean_parameter_name("submission_id", "제출")
REASON_OPTION_NAME = korean_parameter_name("reason", "사유")
SEASON_ID_OPTION_NAME = korean_parameter_name("season_id", "시즌")
RANKING_OPTION_NAME = korean_parameter_name("ranking", "집계")

ROOT_DESCRIPTION = "WIN5 예측과 순위를 관리합니다."
INFO_DESCRIPTION = "현재 활성 WIN5 시즌 정보를 조회합니다."
ROUNDS_DESCRIPTION = "현재 열린 WIN5 라운드를 조회합니다."
SUBMIT_DESCRIPTION = "열린 일반 라운드의 WIN5 예측을 편집합니다."
SUBMIT_ROUND_DESCRIPTION = "예측을 입력할 일반 라운드"
SPECIAL_SUBMIT_DESCRIPTION = "열린 Special 라운드의 gate 예측을 편집합니다."
SPECIAL_SUBMIT_ROUND_DESCRIPTION = "예측을 입력할 Special 라운드"
SUBMISSIONS_DESCRIPTION = "현재 시즌의 WIN5 제출과 채점 결과를 조회합니다."
CANCEL_DESCRIPTION = "열린 라운드의 내 WIN5 제출을 취소합니다."
CANCEL_SUBMISSION_DESCRIPTION = "취소할 accepted Submission"
CANCEL_REASON_DESCRIPTION = "취소 사유 (선택)"
STANDINGS_DESCRIPTION = "WIN5 시즌 순위를 조회합니다."
STANDINGS_SEASON_DESCRIPTION = "조회할 시즌"
STANDINGS_RANKING_DESCRIPTION = "순위 유형"

STAFF_DESCRIPTION = "WIN5 스태프 전용 기능입니다."
STAFF_ROUND_DESCRIPTION = "WIN5 라운드 생성·삭제·상태·결과·채점·특별 취소를 관리합니다."
STAFF_SEASON_DESCRIPTION = "WIN5 시즌 생성·활성화·종료·취소·정보 수정을 관리합니다."
