"""Staff-only correction of one GameAccount's current Persona owner."""

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

STAFF_GAME_ACCOUNT_OWNER_CORRECTION_AUDIT_SCHEMA_VERSION: Final = 1
STAFF_GAME_ACCOUNT_OWNER_CORRECTION_SOURCE: Final = "discord_staff"


class StaffGameAccountOwnerCorrectionAuditType(StrEnum):
    OWNER_REASSIGNED = "game_account_owner_reassigned"


class StaffGameAccountOwnerCorrectionError(ValueError):
    """Base error for rejected GameAccount owner corrections."""


class StaffGameAccountOwnerCorrectionUnavailableError(StaffGameAccountOwnerCorrectionError):
    """The selected Persona or exact GameAccount is unavailable."""


class StaffGameAccountOwnerCorrectionSameOwnerError(StaffGameAccountOwnerCorrectionError):
    """The exact GameAccount already belongs to the target Persona."""


class StaffGameAccountOwnerCorrectionWalletMissingError(StaffGameAccountOwnerCorrectionError):
    """The target Persona is missing its canonical wallet."""


class StaffGameAccountOwnerCorrectionStaleError(StaffGameAccountOwnerCorrectionError):
    """The correction authority changed after Preview."""


class StaffGameAccountOwnerCorrectionIdempotencyConflictError(StaffGameAccountOwnerCorrectionError):
    """The Final interaction key belongs to another mutation."""


class StaffGameAccountOwnerCorrectionConcurrentConflictError(StaffGameAccountOwnerCorrectionError):
    """Another Identity mutation won."""


class StaffGameAccountOwnerCorrectionAuditError(StaffGameAccountOwnerCorrectionError):
    """Stored owner-correction evidence is malformed."""


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


def _payload_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean.")
    return value


@dataclass(frozen=True, slots=True)
class StaffGameAccountOwnerCorrectionState:
    """Detached authority for one exact current-owner correction."""

    guild_id: str
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    source_persona_id: str
    source_persona_display_name: str
    source_persona_status: PersonaStatus
    source_has_wallet: bool
    source_game_account_count: int
    source_qualifying_game_account_count: int
    target_persona_id: str
    target_persona_display_name: str
    target_persona_status: PersonaStatus
    target_has_wallet: bool
    target_game_account_count: int
    target_qualifying_game_account_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "game_account_id", _positive_int(self.game_account_id, field_name="game_account_id"))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        for prefix in ("source", "target"):
            object.__setattr__(self, f"{prefix}_persona_id", _persona_id(getattr(self, f"{prefix}_persona_id")))
            object.__setattr__(
                self,
                f"{prefix}_persona_display_name",
                _text(
                    getattr(self, f"{prefix}_persona_display_name"),
                    field_name=f"{prefix}_persona_display_name",
                    max_length=100,
                ),
            )
            object.__setattr__(
                self,
                f"{prefix}_persona_status",
                PersonaStatus(getattr(self, f"{prefix}_persona_status")),
            )
            has_wallet = getattr(self, f"{prefix}_has_wallet")
            if not isinstance(has_wallet, bool):
                raise ValueError(f"{prefix}_has_wallet must be a boolean.")
            for suffix in ("game_account_count", "qualifying_game_account_count"):
                field_name = f"{prefix}_{suffix}"
                object.__setattr__(
                    self,
                    field_name,
                    _non_negative_int(getattr(self, field_name), field_name=field_name),
                )
            if getattr(self, f"{prefix}_qualifying_game_account_count") > getattr(self, f"{prefix}_game_account_count"):
                raise ValueError(f"{prefix} qualifying account count cannot exceed total account count.")
        if self.source_game_account_count == 0 or self.source_qualifying_game_account_count == 0:
            raise ValueError("The source projection must contain the exact PID-bearing GameAccount.")

    @property
    def source_resulting_game_account_count(self) -> int:
        return self.source_game_account_count - 1

    @property
    def source_resulting_qualifying_game_account_count(self) -> int:
        return self.source_qualifying_game_account_count - 1

    @property
    def target_resulting_game_account_count(self) -> int:
        return self.target_game_account_count + 1

    @property
    def target_resulting_qualifying_game_account_count(self) -> int:
        return self.target_qualifying_game_account_count + 1

    @property
    def source_resulting_member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.source_persona_status)
            and self.source_has_wallet
            and self.source_resulting_qualifying_game_account_count > 0
        )

    @property
    def target_resulting_member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.target_persona_status)
            and self.target_has_wallet
            and self.target_resulting_qualifying_game_account_count > 0
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_GAME_ACCOUNT_OWNER_CORRECTION_AUDIT_SCHEMA_VERSION,
            "source": STAFF_GAME_ACCOUNT_OWNER_CORRECTION_SOURCE,
            "guild_id": self.guild_id,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "source_persona_id": self.source_persona_id,
            "source_persona_display_name": self.source_persona_display_name,
            "source_persona_status": self.source_persona_status.value,
            "source_has_wallet": self.source_has_wallet,
            "source_game_account_count": self.source_game_account_count,
            "source_qualifying_game_account_count": self.source_qualifying_game_account_count,
            "target_persona_id": self.target_persona_id,
            "target_persona_display_name": self.target_persona_display_name,
            "target_persona_status": self.target_persona_status.value,
            "target_has_wallet": self.target_has_wallet,
            "target_game_account_count": self.target_game_account_count,
            "target_qualifying_game_account_count": self.target_qualifying_game_account_count,
        }

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())


@dataclass(frozen=True, slots=True)
class StaffGameAccountOwnerCorrectionPreview:
    state: StaffGameAccountOwnerCorrectionState
    evidence_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaffGameAccountOwnerCorrectionState):
            raise ValueError("state must be a StaffGameAccountOwnerCorrectionState.")
        object.__setattr__(
            self,
            "evidence_reason",
            _text(self.evidence_reason, field_name="evidence_reason", max_length=255),
        )


@dataclass(frozen=True, slots=True)
class CorrectGameAccountOwner:
    guild_id: str
    target_persona_id: str
    game_region: GameRegion
    uma_pid: str
    evidence_reason: str
    corrected_by_discord_user_id: str
    expected_source_persona_id: str
    expected_state_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "target_persona_id", _persona_id(self.target_persona_id))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(
            self,
            "evidence_reason",
            _text(self.evidence_reason, field_name="evidence_reason", max_length=255),
        )
        object.__setattr__(
            self,
            "corrected_by_discord_user_id",
            _discord_user_id(self.corrected_by_discord_user_id),
        )
        object.__setattr__(self, "expected_source_persona_id", _persona_id(self.expected_source_persona_id))
        for field_name, max_length, optional in (
            ("expected_state_fingerprint", 64, False),
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
                "schema": "staff-game-account-owner-correction-command-v1",
                "guild_id": self.guild_id,
                "target_persona_id": self.target_persona_id,
                "game_region": self.game_region.value,
                "uma_pid": self.uma_pid,
                "evidence_reason": self.evidence_reason,
                "corrected_by_discord_user_id": self.corrected_by_discord_user_id,
                "expected_source_persona_id": self.expected_source_persona_id,
                "expected_state_fingerprint": self.expected_state_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class CorrectedGameAccountOwner:
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    source_persona_id: str
    source_persona_display_name: str
    source_persona_status: PersonaStatus
    source_has_wallet: bool
    source_game_account_count: int
    source_qualifying_game_account_count: int
    target_persona_id: str
    target_persona_display_name: str
    target_persona_status: PersonaStatus
    target_has_wallet: bool
    target_game_account_count: int
    target_qualifying_game_account_count: int
    evidence_reason: str
    corrected_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "game_account_id", _positive_int(self.game_account_id, field_name="game_account_id"))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "uma_pid", normalize_registration_pid(self.uma_pid))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        for prefix in ("source", "target"):
            object.__setattr__(self, f"{prefix}_persona_id", _persona_id(getattr(self, f"{prefix}_persona_id")))
            object.__setattr__(
                self,
                f"{prefix}_persona_display_name",
                _text(
                    getattr(self, f"{prefix}_persona_display_name"),
                    field_name=f"{prefix}_persona_display_name",
                    max_length=100,
                ),
            )
            object.__setattr__(
                self,
                f"{prefix}_persona_status",
                PersonaStatus(getattr(self, f"{prefix}_persona_status")),
            )
            has_wallet = getattr(self, f"{prefix}_has_wallet")
            if not isinstance(has_wallet, bool):
                raise ValueError(f"{prefix}_has_wallet must be a boolean.")
            for suffix in ("game_account_count", "qualifying_game_account_count"):
                field_name = f"{prefix}_{suffix}"
                object.__setattr__(
                    self,
                    field_name,
                    _non_negative_int(getattr(self, field_name), field_name=field_name),
                )
            if getattr(self, f"{prefix}_qualifying_game_account_count") > getattr(self, f"{prefix}_game_account_count"):
                raise ValueError(f"{prefix} qualifying account count cannot exceed total account count.")
        if self.target_game_account_count == 0 or self.target_qualifying_game_account_count == 0:
            raise ValueError("The target projection must include the reassigned PID-bearing account.")
        object.__setattr__(
            self,
            "evidence_reason",
            _text(self.evidence_reason, field_name="evidence_reason", max_length=255),
        )
        object.__setattr__(self, "corrected_at", normalize_utc_datetime(self.corrected_at, field_name="corrected_at"))
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    @property
    def source_member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.source_persona_status)
            and self.source_has_wallet
            and self.source_qualifying_game_account_count > 0
        )

    @property
    def target_member_mutation_eligible(self) -> bool:
        return (
            allows_member_mutation(self.target_persona_status)
            and self.target_has_wallet
            and self.target_qualifying_game_account_count > 0
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": STAFF_GAME_ACCOUNT_OWNER_CORRECTION_AUDIT_SCHEMA_VERSION,
            "source": STAFF_GAME_ACCOUNT_OWNER_CORRECTION_SOURCE,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "source_persona_id": self.source_persona_id,
            "source_persona_display_name": self.source_persona_display_name,
            "source_persona_status": self.source_persona_status.value,
            "source_has_wallet": self.source_has_wallet,
            "source_game_account_count": self.source_game_account_count,
            "source_qualifying_game_account_count": self.source_qualifying_game_account_count,
            "source_member_mutation_eligible": self.source_member_mutation_eligible,
            "target_persona_id": self.target_persona_id,
            "target_persona_display_name": self.target_persona_display_name,
            "target_persona_status": self.target_persona_status.value,
            "target_has_wallet": self.target_has_wallet,
            "target_game_account_count": self.target_game_account_count,
            "target_qualifying_game_account_count": self.target_qualifying_game_account_count,
            "target_member_mutation_eligible": self.target_member_mutation_eligible,
            "evidence_reason": self.evidence_reason,
            "corrected_at": self.corrected_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> CorrectedGameAccountOwner:
        if payload.get("schema_version") != STAFF_GAME_ACCOUNT_OWNER_CORRECTION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-correction audit schema version.")
        if payload.get("source") != STAFF_GAME_ACCOUNT_OWNER_CORRECTION_SOURCE:
            raise ValueError("Owner-correction audit source is invalid.")
        result = cls(
            game_account_id=_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_text(payload, "uma_pid"),
            nickname=_payload_text(payload, "nickname"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            source_persona_id=_payload_text(payload, "source_persona_id"),
            source_persona_display_name=_payload_text(payload, "source_persona_display_name"),
            source_persona_status=PersonaStatus(_payload_text(payload, "source_persona_status")),
            source_has_wallet=_payload_bool(payload, "source_has_wallet"),
            source_game_account_count=_non_negative_int(
                payload["source_game_account_count"], field_name="source_game_account_count"
            ),
            source_qualifying_game_account_count=_non_negative_int(
                payload["source_qualifying_game_account_count"],
                field_name="source_qualifying_game_account_count",
            ),
            target_persona_id=_payload_text(payload, "target_persona_id"),
            target_persona_display_name=_payload_text(payload, "target_persona_display_name"),
            target_persona_status=PersonaStatus(_payload_text(payload, "target_persona_status")),
            target_has_wallet=_payload_bool(payload, "target_has_wallet"),
            target_game_account_count=_positive_int(
                payload["target_game_account_count"], field_name="target_game_account_count"
            ),
            target_qualifying_game_account_count=_positive_int(
                payload["target_qualifying_game_account_count"],
                field_name="target_qualifying_game_account_count",
            ),
            evidence_reason=_payload_text(payload, "evidence_reason"),
            corrected_at=datetime.fromisoformat(_payload_text(payload, "corrected_at")),
        )
        if (
            payload.get("source_member_mutation_eligible") is not result.source_member_mutation_eligible
            or payload.get("target_member_mutation_eligible") is not result.target_member_mutation_eligible
        ):
            raise ValueError("Owner-correction audit eligibility is inconsistent.")
        return result


@dataclass(frozen=True, slots=True)
class StoredStaffGameAccountOwnerCorrectionOperation:
    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    after_data: Mapping[str, object] | None


class StaffGameAccountOwnerCorrectionQueryRepository(Protocol):
    def get_state(
        self,
        *,
        guild_id: str,
        target_persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountOwnerCorrectionState | None: ...


class StaffGameAccountOwnerCorrectionQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_game_account_owner_correction(self) -> StaffGameAccountOwnerCorrectionQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffGameAccountOwnerCorrectionQueries:
    query_runner: QueryRunner[StaffGameAccountOwnerCorrectionQueryUnitOfWork]

    def get_preview(
        self,
        *,
        guild_id: str,
        target_persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
        evidence_reason: str,
    ) -> StaffGameAccountOwnerCorrectionPreview:
        normalized_guild_id = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized_guild_id is not None
        state = self.query_runner.run(
            lambda uow: uow.staff_game_account_owner_correction.get_state(
                guild_id=normalized_guild_id,
                target_persona_id=_persona_id(target_persona_id),
                game_region=GameRegion(game_region),
                uma_pid=normalize_registration_pid(uma_pid),
            )
        )
        return StaffGameAccountOwnerCorrectionPreview(
            state=_validate_available_state(state),
            evidence_reason=evidence_reason,
        )


class StaffGameAccountOwnerCorrectionRepository(Protocol):
    def lock_state(
        self,
        *,
        guild_id: str,
        target_persona_id: str,
        expected_source_persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountOwnerCorrectionState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredStaffGameAccountOwnerCorrectionOperation | None: ...

    def reassign_owner(
        self,
        *,
        command: CorrectGameAccountOwner,
        state: StaffGameAccountOwnerCorrectionState,
        corrected_at: datetime,
    ) -> CorrectedGameAccountOwner: ...


class StaffGameAccountOwnerCorrectionUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_game_account_owner_correction(self) -> StaffGameAccountOwnerCorrectionRepository: ...


@dataclass(frozen=True, slots=True)
class StaffGameAccountOwnerCorrectionCommands:
    command_runner: CommandRunner[StaffGameAccountOwnerCorrectionUnitOfWork]
    clock: Callable[[], datetime]

    def correct(self, command: CorrectGameAccountOwner) -> CorrectedGameAccountOwner:
        return self.command_runner.run(lambda uow: self._correct(uow.staff_game_account_owner_correction, command))

    def _correct(
        self,
        repository: StaffGameAccountOwnerCorrectionRepository,
        command: CorrectGameAccountOwner,
    ) -> CorrectedGameAccountOwner:
        corrected_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        state = repository.lock_state(
            guild_id=command.guild_id,
            target_persona_id=command.target_persona_id,
            expected_source_persona_id=command.expected_source_persona_id,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
        )
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        if state is None:
            raise StaffGameAccountOwnerCorrectionUnavailableError(
                "Selected Persona or exact GameAccount is unavailable."
            )
        if state.source_persona_id != command.expected_source_persona_id:
            raise StaffGameAccountOwnerCorrectionStaleError("GameAccount owner changed after Preview.")
        target = _validate_available_state(state)
        if target.state_fingerprint != command.expected_state_fingerprint:
            raise StaffGameAccountOwnerCorrectionStaleError("GameAccount owner-correction authority changed.")
        result = repository.reassign_owner(command=command, state=target, corrected_at=corrected_at)
        if (
            result.game_account_id != target.game_account_id
            or result.game_region is not target.game_region
            or result.uma_pid != target.uma_pid
            or result.nickname != target.nickname
            or result.affiliation != target.affiliation
            or result.source_persona_id != target.source_persona_id
            or result.source_persona_display_name != target.source_persona_display_name
            or result.source_persona_status is not target.source_persona_status
            or result.source_has_wallet is not target.source_has_wallet
            or result.source_game_account_count != target.source_resulting_game_account_count
            or result.source_qualifying_game_account_count != target.source_resulting_qualifying_game_account_count
            or result.target_persona_id != target.target_persona_id
            or result.target_persona_display_name != target.target_persona_display_name
            or result.target_persona_status is not target.target_persona_status
            or result.target_has_wallet is not target.target_has_wallet
            or result.target_game_account_count != target.target_resulting_game_account_count
            or result.target_qualifying_game_account_count != target.target_resulting_qualifying_game_account_count
            or result.evidence_reason != command.evidence_reason
            or result.corrected_at != corrected_at
            or result.exact_retry
        ):
            raise StaffGameAccountOwnerCorrectionAuditError("GameAccount owner-correction receipt is inconsistent.")
        return result

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredStaffGameAccountOwnerCorrectionOperation,
        command: CorrectGameAccountOwner,
    ) -> CorrectedGameAccountOwner:
        if (
            stored.type != StaffGameAccountOwnerCorrectionAuditType.OWNER_REASSIGNED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffGameAccountOwnerCorrectionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffGameAccountOwnerCorrectionAuditError("Owner-correction retry has no receipt payload.")
        try:
            result = CorrectedGameAccountOwner.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffGameAccountOwnerCorrectionAuditError("Owner-correction retry receipt is malformed.") from error
        if (
            stored.persona_id != result.target_persona_id
            or stored.game_account_id != result.game_account_id
            or result.target_persona_id != command.target_persona_id
            or result.source_persona_id != command.expected_source_persona_id
            or result.game_region is not command.game_region
            or result.uma_pid != command.uma_pid
            or result.evidence_reason != command.evidence_reason
        ):
            raise StaffGameAccountOwnerCorrectionAuditError("Owner-correction retry context is malformed.")
        return replace(result, exact_retry=True)


def _validate_available_state(
    state: StaffGameAccountOwnerCorrectionState | None,
) -> StaffGameAccountOwnerCorrectionState:
    if state is None:
        raise StaffGameAccountOwnerCorrectionUnavailableError("Selected Persona or exact GameAccount is unavailable.")
    if state.source_persona_id == state.target_persona_id:
        raise StaffGameAccountOwnerCorrectionSameOwnerError("GameAccount already belongs to the target Persona.")
    if not state.target_has_wallet:
        raise StaffGameAccountOwnerCorrectionWalletMissingError("Target Persona has no canonical Circle Point wallet.")
    return state
