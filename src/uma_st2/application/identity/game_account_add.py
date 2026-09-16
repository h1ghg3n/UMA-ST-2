"""Staff-only addition of one peer GameAccount to an existing Persona."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import GameRegion, PersonaStatus, allows_member_mutation, normalize_registration_pid
from uma_st2.shared import normalize_utc_datetime

STAFF_GAME_ACCOUNT_ADD_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_GAME_ACCOUNT_ADD_SOURCE: Final = "discord_staff"
STAFF_GAME_ACCOUNT_ADD_ALLOWED_STATUSES: Final = frozenset(
    {PersonaStatus.NORMAL, PersonaStatus.WARNING, PersonaStatus.PENDING_APPROVAL}
)


class StaffGameAccountAddAuditType(StrEnum):
    ADDED = "game_account_added"


class StaffGameAccountAddError(ValueError):
    """Base error for rejected peer GameAccount additions."""


class StaffGameAccountAddPersonaNotFoundError(StaffGameAccountAddError):
    """The selected Persona no longer exists."""


class StaffGameAccountAddPersonaRestrictedError(StaffGameAccountAddError):
    """The selected Persona is terminal and cannot gain an account."""


class StaffGameAccountAddPidUnavailableError(StaffGameAccountAddError):
    """The requested region/PID is already owned."""


class StaffGameAccountAddStaleError(StaffGameAccountAddError):
    """The selected authority changed after Preview."""


class StaffGameAccountAddIdempotencyConflictError(StaffGameAccountAddError):
    """The Final interaction key belongs to another mutation."""


class StaffGameAccountAddConcurrentConflictError(StaffGameAccountAddError):
    """Another Identity mutation won."""


class StaffGameAccountAddAuditError(StaffGameAccountAddError):
    """Stored peer-add evidence is malformed."""


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


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
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
class StaffGameAccountAddState:
    """Detached current authority for one proposed peer account addition."""

    guild_id: str
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    has_wallet: bool
    game_account_count: int
    qualifying_game_account_count: int
    game_region: GameRegion
    uma_pid: str
    registered_game_account_id: int | None
    registered_persona_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        if not isinstance(self.has_wallet, bool):
            raise ValueError("has_wallet must be a boolean.")
        for field_name in ("game_account_count", "qualifying_game_account_count"):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(getattr(self, field_name), field_name=field_name),
            )
        if self.qualifying_game_account_count > self.game_account_count:
            raise ValueError("qualifying_game_account_count cannot exceed game_account_count.")
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        if self.registered_game_account_id is not None:
            object.__setattr__(
                self,
                "registered_game_account_id",
                _positive_int(self.registered_game_account_id, field_name="registered_game_account_id"),
            )
        if self.registered_persona_id is not None:
            object.__setattr__(self, "registered_persona_id", _persona_id(self.registered_persona_id))
        if (self.registered_game_account_id is None) != (self.registered_persona_id is None):
            raise ValueError("Registered account ID and owner must be present together.")

    @property
    def resulting_qualifying_game_account_count(self) -> int:
        return self.qualifying_game_account_count + 1

    @property
    def resulting_member_mutation_eligible(self) -> bool:
        return allows_member_mutation(self.persona_status) and self.has_wallet

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_GAME_ACCOUNT_ADD_AUDIT_SCHEMA_VERSION,
            "source": STAFF_GAME_ACCOUNT_ADD_SOURCE,
            "guild_id": self.guild_id,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "has_wallet": self.has_wallet,
            "game_account_count": self.game_account_count,
            "qualifying_game_account_count": self.qualifying_game_account_count,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "registered_game_account_id": self.registered_game_account_id,
            "registered_persona_id": self.registered_persona_id,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffGameAccountAddPreview:
    state: StaffGameAccountAddState
    nickname: str
    affiliation: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffGameAccountAddState):
            raise ValueError("state must be a StaffGameAccountAddState.")
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))


@dataclass(frozen=True, slots=True)
class AddGameAccountToPersona:
    guild_id: str
    target_persona_id: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    reason: str
    added_by_discord_user_id: str
    expected_target_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "added_by_discord_user_id",
            _discord_user_id(self.added_by_discord_user_id),
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
                "schema": "staff-game-account-add-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "game_region": self.game_region.value,
                "uma_pid": self.uma_pid,
                "nickname": self.nickname,
                "affiliation": self.affiliation,
                "reason": self.reason,
                "added_by_discord_user_id": self.added_by_discord_user_id,
                "expected_target_fingerprint": self.expected_target_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class AddedGameAccount:
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    has_wallet: bool
    game_account_count: int
    qualifying_game_account_count: int
    reason: str
    added_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(self, "game_account_id", _positive_int(self.game_account_id, field_name="game_account_id"))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        if not isinstance(self.has_wallet, bool):
            raise ValueError("has_wallet must be a boolean.")
        for field_name in ("game_account_count", "qualifying_game_account_count"):
            object.__setattr__(
                self,
                field_name,
                _positive_int(getattr(self, field_name), field_name=field_name),
            )
        if self.qualifying_game_account_count > self.game_account_count:
            raise ValueError("qualifying_game_account_count cannot exceed game_account_count.")
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(self, "added_at", normalize_utc_datetime(self.added_at, field_name="added_at"))
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    @property
    def member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.persona_status) and self.has_wallet and self.qualifying_game_account_count > 0
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_GAME_ACCOUNT_ADD_AUDIT_SCHEMA_VERSION,
            "source": STAFF_GAME_ACCOUNT_ADD_SOURCE,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "has_wallet": self.has_wallet,
            "game_account_count": self.game_account_count,
            "qualifying_game_account_count": self.qualifying_game_account_count,
            "member_mutation_eligible": self.member_mutation_eligible,
            "reason": self.reason,
            "added_at": self.added_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> AddedGameAccount:
        if payload.get("schema_version") != STAFF_GAME_ACCOUNT_ADD_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported peer-add audit schema version.")
        if payload.get("source") != STAFF_GAME_ACCOUNT_ADD_SOURCE:
            raise ValueError("Peer-add audit source is invalid.")
        result = cls(
            persona_id=_payload_text(payload, "persona_id"),
            persona_display_name=_payload_text(payload, "persona_display_name"),
            persona_status=PersonaStatus(_payload_text(payload, "persona_status")),
            game_account_id=_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_text(payload, "uma_pid"),
            nickname=_payload_text(payload, "nickname"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            has_wallet=payload["has_wallet"],  # type: ignore[arg-type]
            game_account_count=_positive_int(payload["game_account_count"], field_name="game_account_count"),
            qualifying_game_account_count=_positive_int(
                payload["qualifying_game_account_count"], field_name="qualifying_game_account_count"
            ),
            reason=_payload_text(payload, "reason"),
            added_at=datetime.fromisoformat(_payload_text(payload, "added_at")),
        )
        if payload.get("member_mutation_eligible") is not result.member_mutation_eligible:
            raise ValueError("Peer-add audit eligibility is inconsistent.")
        return result


@dataclass(frozen=True, slots=True)
class StoredStaffGameAccountAddOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    after_data: Mapping[str, object] | None


class StaffGameAccountAddQueryRepository(Protocol):
    def get_target_state(
        self, *, guild_id: str, persona_id: str, game_region: GameRegion, uma_pid: str
    ) -> StaffGameAccountAddState | None: ...


class StaffGameAccountAddQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_game_account_add(self) -> StaffGameAccountAddQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffGameAccountAddQueries:
    query_runner: QueryRunner[StaffGameAccountAddQueryUnitOfWork]

    def get_preview(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
        nickname: str,
        affiliation: str | None,
        reason: str,
    ) -> StaffGameAccountAddPreview:
        normalized_guild_id = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized_guild_id is not None
        state = self.query_runner.run(
            lambda uow: uow.staff_game_account_add.get_target_state(
                guild_id=normalized_guild_id,
                persona_id=_persona_id(persona_id),
                game_region=GameRegion(game_region),
                uma_pid=normalize_registration_pid(uma_pid),
            )
        )
        return StaffGameAccountAddPreview(
            state=_validate_available_state(state),
            nickname=nickname,
            affiliation=affiliation,
            reason=reason,
        )


class StaffGameAccountAddRepository(Protocol):
    def lock_target_state(
        self, *, guild_id: str, persona_id: str, game_region: GameRegion, uma_pid: str
    ) -> StaffGameAccountAddState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffGameAccountAddOperation | None: ...

    def add_game_account(
        self,
        *,
        command: AddGameAccountToPersona,
        state: StaffGameAccountAddState,
        added_at: datetime,
    ) -> AddedGameAccount: ...


class StaffGameAccountAddUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_game_account_add(self) -> StaffGameAccountAddRepository: ...


@dataclass(frozen=True, slots=True)
class StaffGameAccountAddCommands:
    command_runner: CommandRunner[StaffGameAccountAddUnitOfWork]
    clock: Callable[[], datetime]

    def add(self, command: AddGameAccountToPersona) -> AddedGameAccount:
        return self.command_runner.run(lambda uow: self._add(uow.staff_game_account_add, command))

    def _add(
        self,
        repository: StaffGameAccountAddRepository,
        command: AddGameAccountToPersona,
    ) -> AddedGameAccount:
        added_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_target_state(
            guild_id=command.guild_id,
            persona_id=command.target_persona_id,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        target = _validate_available_state(state)
        if target.state_fingerprint != command.expected_target_fingerprint:
            raise StaffGameAccountAddStaleError("GameAccount-add authority changed after Preview.")
        result = repository.add_game_account(command=command, state=target, added_at=added_at)
        if (
            result.persona_id != target.persona_id
            or result.persona_display_name != target.persona_display_name
            or result.persona_status is not target.persona_status
            or result.game_region is not command.game_region
            or result.uma_pid != command.uma_pid
            or result.nickname != command.nickname
            or result.affiliation != command.affiliation
            or result.has_wallet is not target.has_wallet
            or result.game_account_count != target.game_account_count + 1
            or result.qualifying_game_account_count != target.qualifying_game_account_count + 1
            or result.reason != command.reason
            or result.added_at != added_at
            or result.exact_retry
        ):
            raise StaffGameAccountAddAuditError("GameAccount-add receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffGameAccountAddOperation,
        command: AddGameAccountToPersona,
    ) -> AddedGameAccount:
        if (
            stored.type != StaffGameAccountAddAuditType.ADDED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffGameAccountAddIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffGameAccountAddAuditError("GameAccount-add retry has no receipt payload.")
        try:
            result = AddedGameAccount.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffGameAccountAddAuditError("GameAccount-add retry receipt is malformed.") from error
        if (
            stored.persona_id != result.persona_id
            or stored.game_account_id != result.game_account_id
            or result.persona_id != command.target_persona_id
            or result.game_region is not command.game_region
            or result.uma_pid != command.uma_pid
            or result.nickname != command.nickname
            or result.affiliation != command.affiliation
            or result.reason != command.reason
        ):
            raise StaffGameAccountAddAuditError("GameAccount-add retry context is malformed.")
        return replace(result, exact_retry=True)


def _validate_available_state(state: StaffGameAccountAddState | None) -> StaffGameAccountAddState:
    if state is None:
        raise StaffGameAccountAddPersonaNotFoundError("Selected Persona does not exist.")
    if state.persona_status not in STAFF_GAME_ACCOUNT_ADD_ALLOWED_STATUSES:
        raise StaffGameAccountAddPersonaRestrictedError("Selected Persona cannot gain a new GameAccount.")
    if state.registered_game_account_id is not None:
        raise StaffGameAccountAddPidUnavailableError("Registration PID is already owned by a GameAccount.")
    return state
