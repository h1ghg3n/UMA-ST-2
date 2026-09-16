"""User-facing copy for Circle Match result-publication recovery."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import MatchResultPublicationTargetChoice, PublishedMatchResult
from uma_st2.domain.publication import PublicationStatus

from ..common import bounded_discord_message, safe_discord_text

PUBLICATION_ALREADY_EXISTS = "이 Match의 최종 결과 publication intent는 이미 존재합니다."
PUBLICATION_UNAVAILABLE = "선택한 Match에는 복구할 settled 결과 공개 intent가 없습니다."
PUBLICATION_IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 결과 공개 내용에 이미 사용되었습니다."

_STATUS_MESSAGES = {
    PublicationStatus.READY: "configured announcement channel의 delivery queue에 등록했습니다.",
    PublicationStatus.AWAITING_CHANNEL: "announcement channel이 없어 awaiting_channel로 보존했습니다.",
    PublicationStatus.SUPPRESSED: "Match announcement가 꺼져 있어 suppressed intent로 보존했습니다.",
}


def match_result_publication_autocomplete_choices(
    targets: tuple[MatchResultPublicationTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert publishable settled Matches into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · {target.grade.value} · settled",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_result_publication_success(result: PublishedMatchResult) -> str:
    """Render a private receipt from committed publication evidence only."""

    status_message = _STATUS_MESSAGES.get(result.publication.status, "durable publication intent를 보존했습니다.")
    return bounded_discord_message(
        (
            "## 룸매치 결과 공개 intent 복구 완료",
            f"Match: {safe_discord_text(result.match_name, limit=400)}",
            f"Match 상태: `{result.match_status.value}`",
            f"Publication: `#{result.publication.publication_id}` · `{result.publication.status.value}`",
            status_message,
            "정산 결과와 Rating·최종 배당률은 committed snapshot에서 전달됩니다.",
        ),
        limit=1900,
    )


def publication_evidence_error(reference_id: str) -> str:
    return f"결과 공개 증거를 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def publication_input_error(error: object) -> str:
    return f"룸매치 결과 공개 intent를 복구하지 못했습니다: {error}"


def publication_internal_error(reference_id: str) -> str:
    return f"룸매치 결과 공개 intent를 복구하지 못했습니다. 참조 ID: `{reference_id}`"
