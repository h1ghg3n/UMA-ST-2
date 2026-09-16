"""User-facing registration copy for the Circle Match command tree."""

from ..localization import korean_parameter_name

MATCH_ID_OPTION_NAME = korean_parameter_name("match_id", "경기")
RANK_OPTION_NAME = korean_parameter_name("rank", "순위")
PERSONA_OPTION_NAME = korean_parameter_name("persona", "페르소나")
BET_ID_OPTION_NAME = korean_parameter_name("bet_id", "베팅")
BET_TYPE_OPTION_NAME = korean_parameter_name("bet_type", "유형")
NUMBERS_OPTION_NAME = korean_parameter_name("numbers", "번호")
AMOUNT_OPTION_NAME = korean_parameter_name("amount", "금액")

MATCH_ROOT_DESCRIPTION = "룸매치 기능입니다."
MATCH_STAFF_GROUP_DESCRIPTION = "룸매치 스태프 전용 기능입니다."

RACE_CREATE_DESCRIPTION = "새 native V2 룸매치를 생성합니다."
RACE_CREATE_GRADE_DESCRIPTION = "경기 등급"
RACE_CREATE_TIMEZONE_DESCRIPTION = "개최일·시각 입력에 적용할 timezone"

RACE_CONDITION_SET_DESCRIPTION = "룸매치 환경 조건을 확정하거나 변경합니다."
RACE_CONDITION_MATCH_DESCRIPTION = "환경 조건을 설정할 룸매치"
RACE_CONDITION_SEASON_DESCRIPTION = "계절"
RACE_CONDITION_WEATHER_DESCRIPTION = "날씨"
RACE_CONDITION_TIME_OF_DAY_DESCRIPTION = "시간대"
RACE_CONDITION_TRACK_DESCRIPTION = "경기장 상태"
RACE_CONDITION_REASON_DESCRIPTION = "기존 조건을 바꾸는 사유 (최초 확정은 선택)"
SEASON_CHOICE_NAMES = ("봄", "여름", "가을", "겨울")
WEATHER_CHOICE_NAMES = ("랜덤", "맑음", "흐림", "비", "눈")
TIME_OF_DAY_CHOICE_NAMES = ("낮", "밤")
TRACK_CONDITION_CHOICE_NAMES = ("랜덤", "양호", "다소 무거움", "포화", "불량")

RACE_ENTRIES_SET_DESCRIPTION = "룸매치 Entry roster 전체를 교체합니다."
RACE_ENTRIES_MATCH_DESCRIPTION = "Entry를 등록하거나 교체할 룸매치"
RACE_ENTRIES_REASON_DESCRIPTION = "Entry roster 교체 사유"

BETTING_OPEN_DESCRIPTION = "룸매치 베팅을 열고 최초 공지 데이터를 저장합니다."
BETTING_OPEN_MATCH_DESCRIPTION = "베팅을 열 native scheduled 룸매치"
BETTING_CLOSE_DESCRIPTION = "룸매치 베팅을 명시적으로 마감합니다."
BETTING_CLOSE_MATCH_DESCRIPTION = "베팅을 마감할 native betting-open 룸매치"

RESULT_SUBMIT_DESCRIPTION = "룸매치의 complete result candidate를 제출합니다."
RESULT_SUBMIT_MATCH_DESCRIPTION = "결과 candidate를 제출할 native 룸매치"
RESULT_SUBMIT_REASON_DESCRIPTION = "확정 결과를 정정하는 사유 (최초·pending 정정은 선택)"
RESULT_REVIEW_DESCRIPTION = "현재 pending 룸매치 결과를 검토합니다."
RESULT_REVIEW_MATCH_DESCRIPTION = "검토하거나 reject할 current pending ResultSubmission"
RESULT_CONFIRM_DESCRIPTION = "현재 pending 룸매치 결과를 권위 결과로 확정합니다."
RESULT_CONFIRM_MATCH_DESCRIPTION = "확정할 current pending ResultSubmission"

SETTLEMENT_DESCRIPTION = "확정 결과와 closed Bet pool을 원자적으로 정산합니다."
SETTLEMENT_MATCH_DESCRIPTION = "정산할 native result-confirmed 룸매치"
SETTLEMENT_REASON_DESCRIPTION = "정산 사유 (선택)"
SETTLEMENT_ROLLBACK_DESCRIPTION = "settled 룸매치를 보상 기록과 함께 terminal void 처리합니다."
SETTLEMENT_ROLLBACK_MATCH_DESCRIPTION = "정산을 terminal rollback할 native settled 룸매치"
SETTLEMENT_ROLLBACK_REASON_DESCRIPTION = "정산 롤백 사유 (필수)"

PUBLISH_DESCRIPTION = "누락된 settled 결과 공개 intent를 복구합니다."
PUBLISH_MATCH_DESCRIPTION = "결과 공개 intent가 누락된 native settled 룸매치"

RACE_CANCEL_DESCRIPTION = "native 룸매치를 전체 취소하고 active Bet을 환불합니다."
RACE_CANCEL_MATCH_DESCRIPTION = "전체 취소할 native pre-settlement 룸매치"
RACE_CANCEL_REASON_DESCRIPTION = "환불 사유 (선택, 입력 시 public 환불 고지에 표시)"

RACES_DESCRIPTION = "현재 룸매치 일정 또는 선택한 경기 상세를 표시합니다."
RACES_MATCH_DESCRIPTION = "상세 조회할 예정 또는 베팅 중인 룸매치 (선택)"
RATINGS_DESCRIPTION = "현재 룸매치 Rating 순위표를 표시합니다."
RATINGS_RANK_DESCRIPTION = "조회 시작 순위 (해당 순위부터 10개 순위, Persona와 동시 사용 불가)"
RATINGS_PERSONA_DESCRIPTION = "Persona 표시명 일부 검색 (순위와 동시 사용 불가)"
BETS_DESCRIPTION = "최근 내 룸매치 베팅 내역을 표시합니다."
BET_DESCRIPTION = "열린 룸매치에 pt를 베팅합니다."
BET_MATCH_DESCRIPTION = "베팅할 native betting-open 룸매치"
BET_TYPE_DESCRIPTION = "베팅 유형"
BET_NUMBERS_DESCRIPTION = "Entry 번호 (쉼표 또는 하이픈 구분)"
BET_AMOUNT_DESCRIPTION = "베팅할 금액 (10 pt 단위, 잔액 기준 상한 적용)"
BET_TYPE_CHOICE_NAMES = ("단승", "복승", "삼복승")

BET_CHANGE_DESCRIPTION = "본인의 active 룸매치 Bet을 새 Bet으로 정정합니다."
BET_CHANGE_TARGET_DESCRIPTION = "정정할 본인의 active Bet"
BET_CHANGE_TYPE_DESCRIPTION = "새 베팅 유형"
BET_CHANGE_NUMBERS_DESCRIPTION = "새 Entry 번호 (쉼표 또는 하이픈 구분)"
BET_CHANGE_AMOUNT_DESCRIPTION = "새 베팅 금액 (10 pt 단위, 환불 후 잔액 기준 상한 적용)"
