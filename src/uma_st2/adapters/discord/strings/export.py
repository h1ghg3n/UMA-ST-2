"""User-facing copy for the Discord export command group."""

from __future__ import annotations

from ..common.responses import safe_discord_text
from ..localization import korean_parameter_name

SEASON_OPTION_NAME = korean_parameter_name("season", "시즌")

CIRCLE_POINT_EXPORT_UNAVAILABLE = (
    "Circle Point XLSX를 생성하지 못했습니다: current wallet 또는 retained 거래 내역을 확인해 주세요."
)
WIN5_SEASON_EXPORT_UNAVAILABLE = "WIN5 시즌 XLSX를 생성하지 못했습니다: 선택한 시즌 또는 저장된 데이터를 확인해 주세요."
MATCH_SEASON_EXPORT_UNAVAILABLE = (
    "Circle Match 시즌 XLSX를 생성하지 못했습니다: 선택한 시즌 또는 저장된 데이터를 확인해 주세요."
)

EXPORT_ROOT_DESCRIPTION = "운영 데이터를 파일로 내보냅니다."
EXPORT_WIN5_GROUP_DESCRIPTION = "WIN5 내보내기입니다."
EXPORT_MATCH_GROUP_DESCRIPTION = "Circle Match 내보내기입니다."
WIN5_SEASON_COMMAND_DESCRIPTION = "WIN5 시즌 전체를 XLSX로 내보냅니다."
WIN5_SEASON_OPTION_DESCRIPTION = "내보낼 active 또는 closed WIN5 시즌"
MATCH_SEASON_COMMAND_DESCRIPTION = "Circle Match 반기 시즌을 XLSX로 내보냅니다."
MATCH_SEASON_OPTION_DESCRIPTION = "내보낼 KST calendar-half 시즌"
CIRCLE_POINT_COMMAND_DESCRIPTION = "현재 서클 포인트와 retained 거래 내역을 내보냅니다."


def match_season_choice_name(name: str) -> str:
    """Render one bounded Match Season autocomplete label."""

    return safe_discord_text(name, limit=100)


def win5_season_choice_name(
    *,
    name: str,
    status: str,
    season_id: int,
    duplicate: bool,
) -> str:
    """Render one bounded WIN5 Season autocomplete label."""

    suffix = f" · {status}"
    if duplicate:
        suffix += f" · ID {season_id}"
    safe_name = safe_discord_text(name, limit=max(1, 100 - len(suffix)))
    return f"{safe_name}{suffix}"


def circle_point_export_success(*, row_count: int, sha256_hex: str) -> str:
    """Render the committed Circle Point export receipt."""

    return f"Circle Point XLSX를 생성했습니다.\n데이터 행: {row_count}\nSHA-256: `{sha256_hex}`"


def win5_season_export_success(
    *,
    scope_name: str,
    row_count: int,
    sha256_hex: str,
) -> str:
    """Render the committed WIN5 Season export receipt."""

    return (
        "WIN5 시즌 XLSX를 생성했습니다.\n"
        f"시즌: {safe_discord_text(scope_name, limit=100)}\n"
        f"데이터 행: {row_count}\n"
        f"SHA-256: `{sha256_hex}`"
    )


def match_season_export_success(
    *,
    scope_name: str,
    row_count: int,
    sha256_hex: str,
) -> str:
    """Render the committed Circle Match Season export receipt."""

    return (
        "Circle Match 시즌 XLSX를 생성했습니다.\n"
        f"시즌: {safe_discord_text(scope_name, limit=100)}\n"
        f"데이터 행: {row_count}\n"
        f"SHA-256: `{sha256_hex}`"
    )
