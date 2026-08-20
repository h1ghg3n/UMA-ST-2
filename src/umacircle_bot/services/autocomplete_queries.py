from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from unicodedata import category

from sqlalchemy import String, case, cast, func, or_, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    DiscordAccount,
    GameAccount,
    Race,
    Win5Entry,
    Win5Round,
    Win5Season,
)
from umacircle_bot.domain.races import MatchRaceStatus
from umacircle_bot.domain.win5 import (
    Win5RoundStatus,
    Win5SeasonStatus,
    Win5SubmissionStatus,
)

MAX_AUTOCOMPLETE_CHOICES = 25
MAX_CHOICE_LABEL_LENGTH = 100
MAX_CHOICE_STATE_LENGTH = 32


class Win5RoundChoicePurpose(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    RESULT = "result"
    SCORE = "score"
    SUBMIT = "submit"


class RoomRaceChoicePurpose(StrEnum):
    SHOW = "show"
    EDIT = "edit"
    ENTRIES = "entries"
    OPEN = "open"
    CLOSE = "close"
    BET = "bet"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class AutocompleteChoice:
    value: int
    label: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("choice value must be a positive integer ID")
        if (
            not isinstance(self.label, str)
            or not self.label
            or len(self.label) > MAX_CHOICE_LABEL_LENGTH
            or any(character.isspace() and character != " " for character in self.label)
            or _has_control_character(self.label)
        ):
            raise ValueError("choice label must be safe bounded text")
        if (
            not isinstance(self.state, str)
            or not self.state
            or len(self.state) > MAX_CHOICE_STATE_LENGTH
            or any(character.isspace() for character in self.state)
            or _has_control_character(self.state)
        ):
            raise ValueError("choice state must be safe bounded text")


_WIN5_ROUND_STATUSES_BY_PURPOSE = {
    Win5RoundChoicePurpose.OPEN: frozenset({Win5RoundStatus.SETUP.value}),
    Win5RoundChoicePurpose.CLOSE: frozenset({Win5RoundStatus.OPEN.value}),
    Win5RoundChoicePurpose.RESULT: frozenset({Win5RoundStatus.CLOSED.value}),
    Win5RoundChoicePurpose.SCORE: frozenset({Win5RoundStatus.RESULT_ENTERED.value}),
    Win5RoundChoicePurpose.SUBMIT: frozenset({Win5RoundStatus.OPEN.value}),
}

_ROOM_RACE_STATUSES_BY_PURPOSE = {
    RoomRaceChoicePurpose.SHOW: frozenset(status.value for status in MatchRaceStatus),
    RoomRaceChoicePurpose.EDIT: frozenset({MatchRaceStatus.SETUP.value}),
    RoomRaceChoicePurpose.ENTRIES: frozenset({MatchRaceStatus.SETUP.value}),
    RoomRaceChoicePurpose.OPEN: frozenset({MatchRaceStatus.SETUP.value}),
    RoomRaceChoicePurpose.CLOSE: frozenset({MatchRaceStatus.BETTING_OPEN.value}),
    RoomRaceChoicePurpose.BET: frozenset({MatchRaceStatus.BETTING_OPEN.value}),
    RoomRaceChoicePurpose.RESULT: frozenset(
        {
            MatchRaceStatus.BETTING_CLOSED.value,
            MatchRaceStatus.RESULT_REVIEW.value,
            MatchRaceStatus.RESULT_CONFIRMED.value,
        }
    ),
}

_WIN5_SEASON_STATUS_LABELS = {
    Win5SeasonStatus.DRAFT.value: "준비 중",
    Win5SeasonStatus.ACTIVE.value: "활성",
    Win5SeasonStatus.CLOSED.value: "종료",
    Win5SeasonStatus.CANCELLED.value: "취소",
}


def autocomplete_staff_win5_seasons(
    session: Session,
    *,
    allowed_statuses: Collection[str | Win5SeasonStatus],
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    statuses = _statuses(
        allowed_statuses,
        known=frozenset(status.value for status in Win5SeasonStatus),
        field="WIN5 season status",
    )
    if not statuses:
        return ()
    statement = select(
        Win5Season.id,
        Win5Season.season_number,
        Win5Season.name,
        Win5Season.status,
    ).where(Win5Season.status.in_(statuses))
    search = _search_clause(query, Win5Season.id, Win5Season.name, Win5Season.status)
    if search is not None:
        statement = statement.where(search)
    status_order = case(
        (Win5Season.status == Win5SeasonStatus.ACTIVE.value, 0),
        (Win5Season.status == Win5SeasonStatus.DRAFT.value, 1),
        else_=2,
    )
    rows = session.execute(statement.order_by(status_order, Win5Season.id.desc()).limit(MAX_AUTOCOMPLETE_CHOICES)).all()
    return tuple(
        _choice(
            row.id,
            f"{row.season_number}시즌 ({_WIN5_SEASON_STATUS_LABELS[row.status]}) · {row.name}",
            row.status,
        )
        for row in rows
    )


def autocomplete_win5_rounds(
    session: Session,
    *,
    season_id: int,
    purpose: str | Win5RoundChoicePurpose,
    query: str = "",
    allowed_statuses: Collection[str | Win5RoundStatus] | None = None,
) -> tuple[AutocompleteChoice, ...]:
    normalized_season_id = _positive_id(season_id, field="season ID")
    normalized_purpose = _enum_value(purpose, Win5RoundChoicePurpose, field="WIN5 round purpose")
    statuses = _purpose_statuses(
        _WIN5_ROUND_STATUSES_BY_PURPOSE[normalized_purpose],
        allowed_statuses,
        known=frozenset(status.value for status in Win5RoundStatus),
        field="WIN5 round status",
    )
    if not statuses:
        return ()
    statement = select(
        Win5Round.id,
        Win5Round.round_number,
        Win5Round.round_label,
        Win5Round.round_type,
        Win5Round.status,
    ).where(
        Win5Round.season_id == normalized_season_id,
        Win5Round.status.in_(statuses),
    )
    search = _search_clause(
        query,
        Win5Round.id,
        Win5Round.round_number,
        Win5Round.round_label,
        Win5Round.round_type,
        Win5Round.status,
    )
    if search is not None:
        statement = statement.where(search)
    rows = session.execute(
        statement.order_by(Win5Round.round_number.desc(), Win5Round.id.desc()).limit(MAX_AUTOCOMPLETE_CHOICES)
    ).all()
    return tuple(
        _choice(
            row.id,
            _win5_round_label(
                round_id=row.id,
                round_number=row.round_number,
                round_label=row.round_label,
                round_type=row.round_type,
                status=row.status,
            ),
            row.status,
        )
        for row in rows
    )


def autocomplete_active_win5_rounds(
    session: Session,
    *,
    purpose: str | Win5RoundChoicePurpose,
) -> tuple[AutocompleteChoice, ...]:
    normalized_purpose = _enum_value(purpose, Win5RoundChoicePurpose, field="WIN5 round purpose")
    statuses = _WIN5_ROUND_STATUSES_BY_PURPOSE[normalized_purpose]
    rows = session.execute(
        select(
            Win5Round.id,
            Win5Round.round_number,
            Win5Round.round_label,
            Race.name.label("race_name"),
            Win5Round.status,
        )
        .join(Win5Season, Win5Season.id == Win5Round.season_id)
        .outerjoin(Race, Race.id == Win5Round.race_id)
        .where(
            Win5Season.status == Win5SeasonStatus.ACTIVE.value,
            Win5Round.status.in_(statuses),
        )
        .order_by(Win5Round.round_number, Win5Round.id)
        .limit(MAX_AUTOCOMPLETE_CHOICES)
    ).all()
    return tuple(
        _choice(
            row.id,
            _win5_lifecycle_round_label(
                round_number=row.round_number,
                round_label=row.round_label,
                race_name=row.race_name,
            ),
            row.status,
        )
        for row in rows
    )


def autocomplete_room_races(
    session: Session,
    *,
    purpose: str | RoomRaceChoicePurpose,
    query: str = "",
    race_kind: str = "room_match",
    allowed_statuses: Collection[str | MatchRaceStatus] | None = None,
) -> tuple[AutocompleteChoice, ...]:
    normalized_purpose = _enum_value(purpose, RoomRaceChoicePurpose, field="Room Match race purpose")
    normalized_race_kind = _bounded_query_text(race_kind, maximum=32)
    if not normalized_race_kind:
        raise ValueError("race kind is required")
    statuses = _purpose_statuses(
        _ROOM_RACE_STATUSES_BY_PURPOSE[normalized_purpose],
        allowed_statuses,
        known=frozenset(status.value for status in MatchRaceStatus),
        field="Room Match race status",
    )
    if not statuses:
        return ()
    statement = select(Race.id, Race.name, Race.status).where(
        Race.race_kind == normalized_race_kind,
        Race.status.in_(statuses),
    )
    if normalized_purpose is not RoomRaceChoicePurpose.SHOW:
        statement = statement.where(Race.external_source.is_(None))
    search = _search_clause(query, Race.id, Race.name, Race.external_race_id, Race.status)
    if search is not None:
        statement = statement.where(search)
    rows = session.execute(statement.order_by(Race.id.desc()).limit(MAX_AUTOCOMPLETE_CHOICES)).all()
    return tuple(
        _choice(
            row.id,
            f"#{row.id} {row.name} · {row.status}",
            row.status,
        )
        for row in rows
    )


def autocomplete_member_cancellable_win5_submissions(
    session: Session,
    *,
    discord_user_id: str,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    owner = _bounded_query_text(discord_user_id, maximum=32)
    if not owner:
        raise ValueError("Discord user ID is required")
    statement = (
        select(
            Win5Entry.id,
            Win5Entry.prediction_tier,
            Win5Entry.status,
            Win5Round.round_number,
            Win5Round.round_label,
        )
        .join(Win5Round, Win5Round.id == Win5Entry.round_id)
        .join(GameAccount, GameAccount.id == Win5Entry.game_account_id)
        .join(DiscordAccount, DiscordAccount.id == GameAccount.discord_account_id)
        .where(
            DiscordAccount.discord_user_id == owner,
            Win5Entry.status == Win5SubmissionStatus.ACCEPTED.value,
            Win5Round.status == Win5RoundStatus.OPEN.value,
        )
    )
    search = _search_clause(
        query,
        Win5Entry.id,
        Win5Entry.prediction_tier,
        Win5Entry.status,
        Win5Round.round_number,
        Win5Round.round_label,
    )
    if search is not None:
        statement = statement.where(search)
    rows = session.execute(statement.order_by(Win5Entry.id.desc()).limit(MAX_AUTOCOMPLETE_CHOICES)).all()
    return tuple(
        _choice(
            row.id,
            _submission_label(
                submission_id=row.id,
                round_number=row.round_number,
                round_label=row.round_label,
                prediction_tier=row.prediction_tier,
                status=row.status,
            ),
            row.status,
        )
        for row in rows
    )


def autocomplete_registered_accounts(
    session: Session,
    *,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    display_name = func.coalesce(
        GameAccount.ingame_name,
        GameAccount.nickname,
        DiscordAccount.discord_nickname,
        "",
    )
    statement = (
        select(
            GameAccount.id,
            GameAccount.uma_pid,
            GameAccount.identity_status,
            display_name.label("display_name"),
        )
        .join(DiscordAccount, DiscordAccount.id == GameAccount.discord_account_id)
        .where(
            GameAccount.identity_status == "confirmed_identity",
            GameAccount.uma_pid.is_not(None),
        )
    )
    search = _search_clause(
        query,
        GameAccount.uma_pid,
        GameAccount.ingame_name,
        GameAccount.nickname,
        DiscordAccount.discord_nickname,
    )
    if search is not None:
        statement = statement.where(search)
    rows = session.execute(
        statement.order_by(GameAccount.uma_pid, func.lower(display_name), GameAccount.id).limit(
            MAX_AUTOCOMPLETE_CHOICES
        )
    ).all()
    return tuple(
        _choice(
            row.id,
            f"({row.uma_pid}) - {row.display_name}",
            row.identity_status,
        )
        for row in rows
    )


def autocomplete_owned_game_accounts(
    session: Session,
    *,
    discord_user_id: str,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    """Return only peer GameAccounts owned by the actor's current Persona."""

    owner = _bounded_query_text(discord_user_id, maximum=32)
    if not owner:
        raise ValueError("Discord user ID is required")
    persona_id = session.scalar(select(DiscordAccount.persona_id).where(DiscordAccount.discord_user_id == owner))
    if persona_id is None:
        return ()
    display_name = func.coalesce(
        GameAccount.ingame_name,
        GameAccount.nickname,
        GameAccount.uma_pid,
        "이름 없음",
    )
    statement = select(
        GameAccount.id,
        GameAccount.uma_pid,
        GameAccount.identity_status,
        display_name.label("display_name"),
    ).where(GameAccount.persona_id == persona_id)
    search = _search_clause(
        query,
        GameAccount.id,
        GameAccount.uma_pid,
        GameAccount.ingame_name,
        GameAccount.nickname,
        GameAccount.identity_status,
    )
    if search is not None:
        statement = statement.where(search)
    rows = session.execute(
        statement.order_by(func.lower(display_name), GameAccount.id).limit(MAX_AUTOCOMPLETE_CHOICES)
    ).all()
    return tuple(
        _choice(
            row.id,
            f"#{row.id} ({row.uma_pid or 'PID 미확정'}) · {row.display_name}",
            row.identity_status,
        )
        for row in rows
    )


def _choice(value: int, label: str, state: str) -> AutocompleteChoice:
    return AutocompleteChoice(
        value=value,
        label=_safe_label(label),
        state=_safe_state(state),
    )


def _win5_round_label(
    *,
    round_id: int,
    round_number: int,
    round_label: str | None,
    round_type: str,
    status: str,
) -> str:
    name = round_label.strip() if isinstance(round_label, str) and round_label.strip() else round_type
    return f"#{round_id} {round_number}R {name} · {status}"


def _win5_lifecycle_round_label(
    *,
    round_number: int,
    round_label: str | None,
    race_name: str | None,
) -> str:
    if isinstance(race_name, str) and race_name.strip():
        name = race_name.strip()
    elif isinstance(round_label, str) and round_label.strip():
        name = round_label.strip()
    else:
        name = "특별 라운드"
    return f"{round_number}R {name}"


def _submission_label(
    *,
    submission_id: int,
    round_number: int,
    round_label: str | None,
    prediction_tier: str,
    status: str,
) -> str:
    round_name = f" {round_label.strip()}" if isinstance(round_label, str) and round_label.strip() else ""
    return f"#{submission_id} {round_number}R{round_name} · {prediction_tier} · {status}"


def _safe_label(value: object) -> str:
    normalized = _single_line(value)
    normalized = normalized.replace("<", "‹").replace(">", "›").replace("@", "＠")
    for marker in ("\\", "*", "_", "~", "`", "|", "[", "]"):
        normalized = normalized.replace(marker, f"\\{marker}")
    bounded = normalized[:MAX_CHOICE_LABEL_LENGTH].rstrip()
    return bounded or "(이름 없음)"


def _safe_state(value: object) -> str:
    normalized = _single_line(value).replace("@", "＠")
    bounded = normalized[:MAX_CHOICE_STATE_LENGTH]
    if not bounded or any(character.isspace() for character in bounded):
        raise ValueError("choice state must be non-empty text without whitespace")
    return bounded


def _single_line(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("choice text must be text")
    without_controls = "".join(
        " " if character.isspace() else "" if category(character).startswith("C") else character for character in value
    )
    return " ".join(without_controls.split())


def _has_control_character(value: str) -> bool:
    return any(category(character).startswith("C") for character in value)


def _search_clause(query: str, *columns):
    normalized = _bounded_query_text(query, maximum=100)
    if not normalized:
        return None
    escaped = normalized.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    pattern = f"%{escaped}%"
    return or_(*[func.lower(cast(column, String)).like(pattern, escape="!") for column in columns])


def _bounded_query_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("query text must be text")
    return _single_line(value)[:maximum]


def _positive_id(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _enum_value(value, enum_type, *, field: str):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc


def _statuses(
    values: Collection,
    *,
    known: frozenset[str],
    field: str,
) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise ValueError(f"{field} collection is invalid")
    normalized = frozenset(value.value if isinstance(value, StrEnum) else value for value in values)
    if any(not isinstance(value, str) or value not in known for value in normalized):
        raise ValueError(f"{field} collection contains an unsupported value")
    return normalized


def _purpose_statuses(
    purpose_statuses: frozenset[str],
    allowed_statuses: Collection | None,
    *,
    known: frozenset[str],
    field: str,
) -> frozenset[str]:
    if allowed_statuses is None:
        return purpose_statuses
    return purpose_statuses & _statuses(allowed_statuses, known=known, field=field)
