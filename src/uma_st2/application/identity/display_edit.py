"""Staff-only Persona and GameAccount display-information corrections."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import GameRegion, PersonaStatus, normalize_registration_pid
from uma_st2.shared import normalize_utc_datetime

STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_DISPLAY_EDIT_SOURCE: Final = "discord_staff"
STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE: Final = 20


class StaffDisplayEditAuditType(StrEnum):
    PERSONA_UPDATED = "persona_display_name_updated"
    GAME_ACCOUNT_UPDATED = "game_account_display_info_updated"


class StaffDisplayEditError(ValueError):
    """Base error for rejected display-information corrections."""


class StaffDisplayEditPersonaNotFoundError(StaffDisplayEditError):
    """The selected Persona no longer exists."""


class StaffDisplayEditGameAccountNotFoundError(StaffDisplayEditError):
    """The selected GameAccount is absent or no longer owned by the Persona."""


class StaffDisplayEditNoChangeError(StaffDisplayEditError):
    """The desired display information equals current canonical state."""


class StaffDisplayEditStaleError(StaffDisplayEditError):
    """The selected authority changed after Preview."""


class StaffDisplayEditIdempotencyConflictError(StaffDisplayEditError):
    """The Final interaction key belongs to another mutation."""


class StaffDisplayEditConcurrentConflictError(StaffDisplayEditError):
    """Another Identity mutation won."""


class StaffDisplayEditAuditError(StaffDisplayEditError):
    """Stored display-edit evidence is malformed."""


def _text(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


def _persona_id(value: str) -> str:
    normalized = _text(value, field_name="persona_id", max_length=36)
    assert normalized is not None
    return normalized


def _discord_user_id(value: str) -> str:
    normalized = _text(value, field_name="discord_user_id", max_length=32)
    assert normalized is not None
    if not normalized.isascii() or not normalized.isdecimal() or normalized.startswith("0") or int(normalized) <= 0:
        raise ValueError("discord_user_id must be a positive ASCII Discord snowflake.")
    return normalized


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be non-empty text.")
    return value


def _payload_optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{key} must be non-empty text or null.")
    return value


@dataclass(frozen=True, slots=True)
class StaffPersonaDisplayState:
    guild_id: str
    persona_id: str
    display_name: str
    status: PersonaStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISPLAY_EDIT_SOURCE,
            "guild_id": self.guild_id,
            "persona_id": self.persona_id,
            "display_name": self.display_name,
            "status": self.status.value,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffGameAccountDisplayState:
    guild_id: str
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    game_account_id: int
    game_region: GameRegion
    uma_pid: str | None
    nickname: str
    affiliation: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        if self.uma_pid is not None:
            object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISPLAY_EDIT_SOURCE,
            "guild_id": self.guild_id,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffDisplayGameAccountPage:
    persona: StaffPersonaDisplayState
    items: tuple[StaffGameAccountDisplayState, ...]
    page: int
    total_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.persona, StaffPersonaDisplayState):
            raise ValueError("persona must be a StaffPersonaDisplayState.")
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, StaffGameAccountDisplayState) for item in self.items):
            raise ValueError("items must contain StaffGameAccountDisplayState values.")
        if any(item.persona_id != self.persona.persona_id for item in self.items):
            raise ValueError("Every account must belong to the page Persona.")
        object.__setattr__(self, "page", _non_negative_int(self.page, field_name="page"))
        object.__setattr__(self, "total_count", _non_negative_int(self.total_count, field_name="total_count"))
        if len(self.items) > STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE or len(self.items) > self.total_count:
            raise ValueError("GameAccount page cardinality is invalid.")

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE < self.total_count


@dataclass(frozen=True, slots=True)
class StaffPersonaDisplayPreview:
    state: StaffPersonaDisplayState
    display_name: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffPersonaDisplayState):
            raise ValueError("state must be a StaffPersonaDisplayState.")
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))


@dataclass(frozen=True, slots=True)
class StaffGameAccountDisplayPreview:
    state: StaffGameAccountDisplayState
    nickname: str
    affiliation: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffGameAccountDisplayState):
            raise ValueError("state must be a StaffGameAccountDisplayState.")
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))


@dataclass(frozen=True, slots=True)
class UpdatePersonaDisplayName:
    guild_id: str
    target_persona_id: str
    display_name: str
    reason: str
    updated_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "updated_by_discord_user_id",
            _discord_user_id(self.updated_by_discord_user_id),
        )
        for field_name, max_length, optional in (
            ("expected_target_fingerprint", 64, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name, max_length=max_length, optional=optional),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-persona-display-name-update-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "display_name": self.display_name,
                "reason": self.reason,
                "updated_by_discord_user_id": self.updated_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class UpdateGameAccountDisplayInfo:
    guild_id: str
    target_persona_id: str
    game_account_id: int
    nickname: str
    affiliation: str | None
    reason: str
    updated_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "updated_by_discord_user_id",
            _discord_user_id(self.updated_by_discord_user_id),
        )
        for field_name, max_length, optional in (
            ("expected_target_fingerprint", 64, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name, max_length=max_length, optional=optional),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "staff-game-account-display-info-update-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "game_account_id": self.game_account_id,
                "nickname": self.nickname,
                "affiliation": self.affiliation,
                "reason": self.reason,
                "updated_by_discord_user_id": self.updated_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class UpdatedPersonaDisplayName:
    persona_id: str
    previous_display_name: str
    display_name: str
    status: PersonaStatus
    reason: str
    updated_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "previous_display_name",
            _text(self.previous_display_name, field_name="previous_display_name", max_length=100),
        )
        object.__setattr__(self, "display_name", _text(self.display_name, field_name="display_name", max_length=100))
        object.__setattr__(self, "status", PersonaStatus(self.status))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "updated_at", normalize_utc_datetime(self.updated_at, field_name="updated_at"))
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISPLAY_EDIT_SOURCE,
            "persona_id": self.persona_id,
            "previous_display_name": self.previous_display_name,
            "display_name": self.display_name,
            "status": self.status.value,
            "reason": self.reason,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> UpdatedPersonaDisplayName:
        _validate_receipt_header(payload)
        return cls(
            persona_id=_payload_text(payload, "persona_id"),
            previous_display_name=_payload_text(payload, "previous_display_name"),
            display_name=_payload_text(payload, "display_name"),
            status=PersonaStatus(_payload_text(payload, "status")),
            reason=_payload_text(payload, "reason"),
            updated_at=datetime.fromisoformat(_payload_text(payload, "updated_at")),
        )


@dataclass(frozen=True, slots=True)
class UpdatedGameAccountDisplayInfo:
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    game_account_id: int
    game_region: GameRegion
    uma_pid: str | None
    previous_nickname: str
    nickname: str
    previous_affiliation: str | None
    affiliation: str | None
    reason: str
    updated_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        if self.uma_pid is not None:
            object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        for field_name in ("previous_nickname", "nickname"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name=field_name, max_length=100)
            )
        for field_name in ("previous_affiliation", "affiliation"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name, max_length=100, optional=True),
            )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "updated_at", normalize_utc_datetime(self.updated_at, field_name="updated_at"))
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION,
            "source": STAFF_DISPLAY_EDIT_SOURCE,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "previous_nickname": self.previous_nickname,
            "nickname": self.nickname,
            "previous_affiliation": self.previous_affiliation,
            "affiliation": self.affiliation,
            "reason": self.reason,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> UpdatedGameAccountDisplayInfo:
        _validate_receipt_header(payload)
        return cls(
            persona_id=_payload_text(payload, "persona_id"),
            persona_display_name=_payload_text(payload, "persona_display_name"),
            persona_status=PersonaStatus(_payload_text(payload, "persona_status")),
            game_account_id=_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_optional_text(payload, "uma_pid"),
            previous_nickname=_payload_text(payload, "previous_nickname"),
            nickname=_payload_text(payload, "nickname"),
            previous_affiliation=_payload_optional_text(payload, "previous_affiliation"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            reason=_payload_text(payload, "reason"),
            updated_at=datetime.fromisoformat(_payload_text(payload, "updated_at")),
        )


def _validate_receipt_header(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != STAFF_DISPLAY_EDIT_AUDIT_SCHEMA_VERSION:
        raise ValueError("Unsupported display-edit audit schema version.")
    if payload.get("source") != STAFF_DISPLAY_EDIT_SOURCE:
        raise ValueError("Display-edit audit source is invalid.")


@dataclass(frozen=True, slots=True)
class StoredStaffDisplayEditOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    after_data: Mapping[str, object] | None


class StaffDisplayEditQueryRepository(Protocol):
    def get_persona_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaDisplayState | None: ...

    def list_game_accounts(
        self, *, guild_id: str, persona_id: str, offset: int, limit: int
    ) -> StaffDisplayGameAccountPage | None: ...

    def get_game_account_state(
        self, *, guild_id: str, persona_id: str, game_account_id: int
    ) -> StaffGameAccountDisplayState | None: ...


class StaffDisplayEditQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_display_edit(self) -> StaffDisplayEditQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDisplayEditQueries:
    query_runner: QueryRunner[StaffDisplayEditQueryUnitOfWork]

    def get_persona_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaDisplayState:
        state = self.query_runner.run(
            lambda uow: uow.staff_display_edit.get_persona_state(
                guild_id=_required_guild_id(guild_id),
                persona_id=_persona_id(persona_id),
            )
        )
        if state is None:
            raise StaffDisplayEditPersonaNotFoundError("Selected Persona does not exist.")
        return state

    def get_persona_preview(
        self, *, guild_id: str, persona_id: str, display_name: str, reason: str
    ) -> StaffPersonaDisplayPreview:
        state = self.get_persona_state(guild_id=guild_id, persona_id=persona_id)
        preview = StaffPersonaDisplayPreview(state=state, display_name=display_name, reason=reason)
        if preview.display_name == state.display_name:
            raise StaffDisplayEditNoChangeError("Persona display name is unchanged.")
        return preview

    def list_game_accounts(self, *, guild_id: str, persona_id: str, page: int = 0) -> StaffDisplayGameAccountPage:
        normalized_page = _non_negative_int(page, field_name="page")
        result = self.query_runner.run(
            lambda uow: uow.staff_display_edit.list_game_accounts(
                guild_id=_required_guild_id(guild_id),
                persona_id=_persona_id(persona_id),
                offset=normalized_page * STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE,
                limit=STAFF_DISPLAY_GAME_ACCOUNT_PAGE_SIZE,
            )
        )
        if result is None:
            raise StaffDisplayEditPersonaNotFoundError("Selected Persona does not exist.")
        if result.page != normalized_page:
            raise StaffDisplayEditAuditError("GameAccount page cursor is inconsistent.")
        return result

    def get_game_account_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_account_id: int,
        nickname: str,
        affiliation: str | None,
        reason: str,
    ) -> StaffGameAccountDisplayPreview:
        state = self.query_runner.run(
            lambda uow: uow.staff_display_edit.get_game_account_state(
                guild_id=_required_guild_id(guild_id),
                persona_id=_persona_id(persona_id),
                game_account_id=_positive_int(game_account_id, field_name="game_account_id"),
            )
        )
        if state is None:
            raise StaffDisplayEditGameAccountNotFoundError("GameAccount is unavailable.")
        preview = StaffGameAccountDisplayPreview(
            state=state,
            nickname=nickname,
            affiliation=affiliation,
            reason=reason,
        )
        if preview.nickname == state.nickname and preview.affiliation == state.affiliation:
            raise StaffDisplayEditNoChangeError("GameAccount display information is unchanged.")
        return preview


class StaffDisplayEditRepository(Protocol):
    def lock_persona_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaDisplayState | None: ...

    def lock_game_account_state(
        self, *, guild_id: str, persona_id: str, game_account_id: int
    ) -> StaffGameAccountDisplayState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDisplayEditOperation | None: ...

    def update_persona(
        self,
        *,
        command: UpdatePersonaDisplayName,
        state: StaffPersonaDisplayState,
        updated_at: datetime,
    ) -> UpdatedPersonaDisplayName: ...

    def update_game_account(
        self,
        *,
        command: UpdateGameAccountDisplayInfo,
        state: StaffGameAccountDisplayState,
        updated_at: datetime,
    ) -> UpdatedGameAccountDisplayInfo: ...


class StaffDisplayEditUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_display_edit(self) -> StaffDisplayEditRepository: ...


@dataclass(frozen=True, slots=True)
class StaffDisplayEditCommands:
    command_runner: CommandRunner[StaffDisplayEditUnitOfWork]
    clock: Callable[[], datetime]

    def update_persona(self, command: UpdatePersonaDisplayName) -> UpdatedPersonaDisplayName:
        return self.command_runner.run(lambda uow: self._update_persona(uow.staff_display_edit, command))

    def update_game_account(self, command: UpdateGameAccountDisplayInfo) -> UpdatedGameAccountDisplayInfo:
        return self.command_runner.run(lambda uow: self._update_game_account(uow.staff_display_edit, command))

    def _update_persona(
        self,
        repository: StaffDisplayEditRepository,
        command: UpdatePersonaDisplayName,
    ) -> UpdatedPersonaDisplayName:
        updated_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_persona_state(
            guild_id=command.guild_id,
            persona_id=command.target_persona_id,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_persona_retry(stored=stored, command=command)
        if state is None:
            raise StaffDisplayEditPersonaNotFoundError("Selected Persona does not exist.")
        if state.state_fingerprint != command.expected_target_fingerprint:
            raise StaffDisplayEditStaleError("Persona display authority changed after Preview.")
        if state.display_name == command.display_name:
            raise StaffDisplayEditNoChangeError("Persona display name is unchanged.")
        result = repository.update_persona(command=command, state=state, updated_at=updated_at)
        if (
            result.persona_id != state.persona_id
            or result.previous_display_name != state.display_name
            or result.display_name != command.display_name
            or result.status is not state.status
            or result.reason != command.reason
            or result.updated_at != updated_at
            or result.exact_retry
        ):
            raise StaffDisplayEditAuditError("Persona display-edit receipt is inconsistent.")
        return result

    def _update_game_account(
        self,
        repository: StaffDisplayEditRepository,
        command: UpdateGameAccountDisplayInfo,
    ) -> UpdatedGameAccountDisplayInfo:
        updated_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_game_account_state(
            guild_id=command.guild_id,
            persona_id=command.target_persona_id,
            game_account_id=command.game_account_id,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_game_account_retry(stored=stored, command=command)
        if state is None:
            raise StaffDisplayEditGameAccountNotFoundError("GameAccount is unavailable.")
        if state.state_fingerprint != command.expected_target_fingerprint:
            raise StaffDisplayEditStaleError("GameAccount display authority changed after Preview.")
        if state.nickname == command.nickname and state.affiliation == command.affiliation:
            raise StaffDisplayEditNoChangeError("GameAccount display information is unchanged.")
        result = repository.update_game_account(command=command, state=state, updated_at=updated_at)
        if (
            result.persona_id != state.persona_id
            or result.persona_display_name != state.persona_display_name
            or result.persona_status is not state.persona_status
            or result.game_account_id != state.game_account_id
            or result.game_region is not state.game_region
            or result.uma_pid != state.uma_pid
            or result.previous_nickname != state.nickname
            or result.nickname != command.nickname
            or result.previous_affiliation != state.affiliation
            or result.affiliation != command.affiliation
            or result.reason != command.reason
            or result.updated_at != updated_at
            or result.exact_retry
        ):
            raise StaffDisplayEditAuditError("GameAccount display-edit receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_persona_retry(
        *,
        stored: StoredStaffDisplayEditOperation,
        command: UpdatePersonaDisplayName,
    ) -> UpdatedPersonaDisplayName:
        if (
            stored.type != StaffDisplayEditAuditType.PERSONA_UPDATED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffDisplayEditIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffDisplayEditAuditError("Persona display-edit retry has no receipt.")
        try:
            result = UpdatedPersonaDisplayName.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffDisplayEditAuditError("Persona display-edit retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.game_account_id is not None
            or result.persona_id != command.target_persona_id
            or result.display_name != command.display_name
            or result.reason != command.reason
        ):
            raise StaffDisplayEditAuditError("Persona display-edit retry context is malformed.")
        return replace(result, exact_retry=True)

    @staticmethod
    def _resolve_game_account_retry(
        *,
        stored: StoredStaffDisplayEditOperation,
        command: UpdateGameAccountDisplayInfo,
    ) -> UpdatedGameAccountDisplayInfo:
        if (
            stored.type != StaffDisplayEditAuditType.GAME_ACCOUNT_UPDATED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffDisplayEditIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffDisplayEditAuditError("GameAccount display-edit retry has no receipt.")
        try:
            result = UpdatedGameAccountDisplayInfo.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffDisplayEditAuditError("GameAccount display-edit retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.game_account_id != result.game_account_id
            or result.persona_id != command.target_persona_id
            or result.game_account_id != command.game_account_id
            or result.nickname != command.nickname
            or result.affiliation != command.affiliation
            or result.reason != command.reason
        ):
            raise StaffDisplayEditAuditError("GameAccount display-edit retry context is malformed.")
        return replace(result, exact_retry=True)


def _required_guild_id(value: str) -> str:
    normalized = _text(value, field_name="guild_id", max_length=32)
    assert normalized is not None
    return normalized
