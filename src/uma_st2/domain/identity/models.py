"""Domain entities and transitions for identity invariants."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .enums import GameRegion, PersonaStatus, RegistrationRequestStatus
from .errors import (
    DiscordAccountInvariantError,
    GameAccountInvariantError,
    PersonaInvariantError,
    RegistrationRequestInvariantError,
)


def _require_string(
    value: str,
    *,
    field_name: str,
    error_type: type[Exception],
) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a string.")

    return value


def _normalize_enum[EnumType](
    value: object,
    enum_type: type[EnumType],
    *,
    field_name: str,
    error_type: type[Exception],
) -> EnumType:
    """Normalize canonical enum values with strict domain validation."""

    if isinstance(value, enum_type):
        return value

    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a canonical enum value.")

    try:
        return enum_type(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise error_type(
            f"{field_name} must be one of {[member.value for member in enum_type]}; got {value!r}.",
        ) from exc


def _normalize_game_region(
    game_region: GameRegion | str,
    *,
    error_type: type[Exception] = GameAccountInvariantError,
) -> GameRegion:
    return _normalize_enum(
        game_region,
        GameRegion,
        field_name="game_region",
        error_type=error_type,
    )


@dataclass(frozen=True, slots=True)
class Persona:
    """Persona is the canonical owner for points, betting, and WIN5."""

    id: str
    display_name: str
    status: PersonaStatus = PersonaStatus.NORMAL

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            _require_string(self.id, field_name="id", error_type=PersonaInvariantError),
        )
        object.__setattr__(
            self,
            "display_name",
            _require_string(
                self.display_name,
                field_name="display_name",
                error_type=PersonaInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_enum(
                self.status,
                PersonaStatus,
                field_name="status",
                error_type=PersonaInvariantError,
            ),
        )


@dataclass(frozen=True, slots=True)
class DiscordAccount:
    """Discord identity actor for commands and audit."""

    id: int
    discord_user_id: str
    persona_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discord_user_id",
            _require_string(
                self.discord_user_id,
                field_name="discord_user_id",
                error_type=DiscordAccountInvariantError,
            ),
        )

        if self.persona_id is not None:
            object.__setattr__(
                self,
                "persona_id",
                _require_string(
                    self.persona_id,
                    field_name="persona_id",
                    error_type=DiscordAccountInvariantError,
                ),
            )

    @property
    def is_linked(self) -> bool:
        return self.persona_id is not None

    def link_to_persona(self, persona_id: str) -> DiscordAccount:
        persona_id = _require_string(
            persona_id,
            field_name="persona_id",
            error_type=DiscordAccountInvariantError,
        )
        if self.persona_id == persona_id:
            return self
        return replace(self, persona_id=persona_id)

    def unlink(self) -> DiscordAccount:
        return replace(self, persona_id=None)


@dataclass(frozen=True, slots=True)
class GameAccount:
    """Canonical external-game account identity."""

    id: int
    persona_id: str
    game_region: GameRegion
    uma_pid: str | None
    nickname: str
    affiliation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "persona_id",
            _require_string(
                self.persona_id,
                field_name="persona_id",
                error_type=GameAccountInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "game_region",
            _normalize_game_region(self.game_region, error_type=GameAccountInvariantError),
        )
        if self.uma_pid is not None:
            object.__setattr__(
                self,
                "uma_pid",
                _require_string(
                    self.uma_pid,
                    field_name="uma_pid",
                    error_type=GameAccountInvariantError,
                ),
            )
        object.__setattr__(
            self,
            "nickname",
            _require_string(
                self.nickname,
                field_name="nickname",
                error_type=GameAccountInvariantError,
            ),
        )

    @property
    def identity_key(self) -> tuple[GameRegion, str] | None:
        if self.uma_pid is None:
            return None
        return (self.game_region, self.uma_pid)


@dataclass(frozen=True, slots=True)
class GameAccountRegistrationRequest:
    """Request artifact for onboarding workflow."""

    id: int
    guild_id: str
    requester_discord_user_id: str
    discord_display_name_snapshot: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    created_at: datetime
    affiliation: str | None = None
    status: RegistrationRequestStatus = RegistrationRequestStatus.PENDING
    reason: str | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _require_string(
                self.guild_id,
                field_name="guild_id",
                error_type=RegistrationRequestInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "requester_discord_user_id",
            _require_string(
                self.requester_discord_user_id,
                field_name="requester_discord_user_id",
                error_type=RegistrationRequestInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "discord_display_name_snapshot",
            _require_string(
                self.discord_display_name_snapshot,
                field_name="discord_display_name_snapshot",
                error_type=RegistrationRequestInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "game_region",
            _normalize_game_region(self.game_region, error_type=RegistrationRequestInvariantError),
        )
        object.__setattr__(
            self,
            "uma_pid",
            _require_string(
                self.uma_pid,
                field_name="uma_pid",
                error_type=RegistrationRequestInvariantError,
            ),
        )
        object.__setattr__(
            self,
            "nickname",
            _require_string(
                self.nickname,
                field_name="nickname",
                error_type=RegistrationRequestInvariantError,
            ),
        )
        if self.affiliation is not None:
            object.__setattr__(
                self,
                "affiliation",
                _require_string(
                    self.affiliation,
                    field_name="affiliation",
                    error_type=RegistrationRequestInvariantError,
                ),
            )

        object.__setattr__(
            self,
            "status",
            _normalize_enum(
                self.status,
                RegistrationRequestStatus,
                field_name="status",
                error_type=RegistrationRequestInvariantError,
            ),
        )

    @property
    def is_pending(self) -> bool:
        return self.status == RegistrationRequestStatus.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == RegistrationRequestStatus.APPROVED

    @property
    def is_cancelled(self) -> bool:
        return self.status == RegistrationRequestStatus.CANCELLED


def normalize_region(value: GameRegion | str) -> GameRegion:
    """Normalize user-provided values to canonical GameRegion."""

    return _normalize_game_region(value, error_type=RegistrationRequestInvariantError)
