"""Staff review, approval, and rejection of native registration requests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus
from uma_st2.shared import normalize_utc_datetime

from .registration_bootstrap import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION,
    AccountRegistrationBootstrapService,
    CreateAccountRegistrationBootstrap,
    RegistrationBootstrapAuditError,
    RegistrationBootstrapConcurrentConflictError,
    RegistrationBootstrapIdempotencyConflictError,
    RegistrationBootstrapPidUnavailableError,
    RegistrationBootstrapRepository,
)
from .registration_commands import AccountRegistrationAuditType

STAFF_REGISTRATION_REQUEST_PAGE_SIZE: Final = 10
ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION: Final = 1


class StaffRegistrationReviewError(ValueError):
    """Base error for rejected staff registration-review operations."""


class StaffRegistrationRequestNotFoundError(StaffRegistrationReviewError):
    """The selected request or Persona context is not available."""


class StaffRegistrationRequestNotPendingError(StaffRegistrationReviewError):
    """The selected request is already terminal."""


class StaffRegistrationRequestStaleError(StaffRegistrationReviewError):
    """The selected request changed after its Preview was rendered."""


class StaffRegistrationRequesterLinkedError(StaffRegistrationReviewError):
    """The requester gained a Persona link after submitting the request."""


class StaffRegistrationPidUnavailableError(StaffRegistrationReviewError):
    """The request PID is already owned by a canonical GameAccount."""


class StaffRegistrationReviewIdempotencyConflictError(StaffRegistrationReviewError):
    """The interaction key is bound to another logical mutation."""


class StaffRegistrationReviewConcurrentConflictError(StaffRegistrationReviewError):
    """A concurrent review won and this transaction was safely rolled back."""


class StaffRegistrationReviewAuditError(StaffRegistrationReviewError):
    """Stored review evidence is incomplete or internally inconsistent."""


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


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _persona_id(value: str) -> str:
    normalized = _text(value, field_name="persona_id", max_length=36)
    assert normalized is not None
    return normalized


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
class StaffPersonaChoice:
    """One bounded Persona autocomplete result."""

    persona_id: str
    display_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, field_name="display_name", max_length=100),
        )


@dataclass(frozen=True, slots=True)
class StaffPersonaContext:
    """Optional existing Persona context shown by the workflow panel."""

    persona_id: str
    display_name: str
    status: PersonaStatus
    game_account_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, field_name="display_name", max_length=100),
        )
        object.__setattr__(self, "status", PersonaStatus(self.status))
        object.__setattr__(
            self,
            "game_account_count",
            _non_negative_int(self.game_account_count, field_name="game_account_count"),
        )


@dataclass(frozen=True, slots=True)
class StaffPersonaPanel:
    """Detached initial panel projection."""

    pending_request_count: int
    selected_persona: StaffPersonaContext | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pending_request_count",
            _non_negative_int(self.pending_request_count, field_name="pending_request_count"),
        )
        if self.selected_persona is not None and not isinstance(self.selected_persona, StaffPersonaContext):
            raise ValueError("selected_persona must be a StaffPersonaContext or null.")


@dataclass(frozen=True, slots=True)
class StaffRegistrationRequestState:
    """Detached complete request facts used by Preview and final revalidation."""

    request_id: int
    discord_account_id: int
    guild_id: str
    requester_discord_user_id: str
    discord_display_name_snapshot: str
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    status: RegistrationRequestStatus
    active_marker: bool | None
    reason: str | None
    created_at: datetime
    resolved_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(
            self,
            "discord_account_id",
            _positive_int(self.discord_account_id, field_name="discord_account_id"),
        )
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("requester_discord_user_id", 32, False),
            ("discord_display_name_snapshot", 100, False),
            ("uma_pid", 32, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
            ("reason", 255, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "status", RegistrationRequestStatus(self.status))
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )
        if self.resolved_at is not None:
            object.__setattr__(
                self,
                "resolved_at",
                normalize_utc_datetime(self.resolved_at, field_name="resolved_at"),
            )
        if self.status is RegistrationRequestStatus.PENDING:
            if self.active_marker is not True or self.reason is not None or self.resolved_at is not None:
                raise ValueError("Pending request state must own the active marker and have no resolution.")
        elif self.active_marker is not None or self.resolved_at is None:
            raise ValueError("Terminal request state must release the active marker and have a resolution time.")

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(self.to_audit_payload())

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION,
            "request_id": self.request_id,
            "discord_account_id": self.discord_account_id,
            "guild_id": self.guild_id,
            "requester_discord_user_id": self.requester_discord_user_id,
            "discord_display_name_snapshot": self.discord_display_name_snapshot,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "status": self.status.value,
            "active_marker": self.active_marker,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "resolved_at": None if self.resolved_at is None else self.resolved_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class StaffRegistrationRequestPage:
    """One stable ten-row pending-request page."""

    page: int
    total_count: int
    items: tuple[StaffRegistrationRequestState, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "page", _non_negative_int(self.page, field_name="page"))
        object.__setattr__(self, "total_count", _non_negative_int(self.total_count, field_name="total_count"))
        object.__setattr__(self, "items", tuple(self.items))
        if len(self.items) > STAFF_REGISTRATION_REQUEST_PAGE_SIZE or any(
            not isinstance(item, StaffRegistrationRequestState) or item.status is not RegistrationRequestStatus.PENDING
            for item in self.items
        ):
            raise ValueError("Registration request page contains invalid or excessive items.")

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * STAFF_REGISTRATION_REQUEST_PAGE_SIZE < self.total_count


@dataclass(frozen=True, slots=True)
class RegistrationRequestLocator:
    """Unlocked locator used before the requester-first lock root."""

    request_id: int
    guild_id: str
    requester_discord_user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(
            self,
            "requester_discord_user_id",
            _text(
                self.requester_discord_user_id,
                field_name="requester_discord_user_id",
                max_length=32,
            ),
        )


@dataclass(frozen=True, slots=True)
class RegistrationReviewRequester:
    """Locked requester DiscordAccount authority."""

    discord_account_id: int
    discord_user_id: str
    persona_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discord_account_id",
            _positive_int(self.discord_account_id, field_name="discord_account_id"),
        )
        object.__setattr__(
            self,
            "discord_user_id",
            _text(self.discord_user_id, field_name="discord_user_id", max_length=32),
        )
        if self.persona_id is not None:
            object.__setattr__(self, "persona_id", _persona_id(self.persona_id))


@dataclass(frozen=True, slots=True)
class StoredRegistrationReviewOperation:
    """Persisted operation facts required for exact retry."""

    request_fingerprint: str | None
    type: str | None
    persona_id: str | None
    game_account_id: int | None
    discord_user_id: str | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ApproveAccountRegistrationRequest:
    """Approve one exact pending-request Preview."""

    request_id: int
    guild_id: str
    reviewed_by_discord_user_id: str
    expected_request_fingerprint: str
    idempotency_key: str
    correlation_id: str | None = None
    review_note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("reviewed_by_discord_user_id", 32, False),
            ("expected_request_fingerprint", 64, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
            ("review_note", 255, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "account-registration-approval-command-v1",
                "request_id": self.request_id,
                "guild_id": self.guild_id,
                "reviewed_by_discord_user_id": self.reviewed_by_discord_user_id,
                "expected_request_fingerprint": self.expected_request_fingerprint,
                "review_note": self.review_note,
            }
        )


@dataclass(frozen=True, slots=True)
class RejectAccountRegistrationRequest:
    """Reject one exact pending-request Preview with a required reason."""

    request_id: int
    guild_id: str
    reviewed_by_discord_user_id: str
    expected_request_fingerprint: str
    reason: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("reviewed_by_discord_user_id", 32, False),
            ("expected_request_fingerprint", 64, False),
            ("reason", 255, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "account-registration-rejection-command-v1",
                "request_id": self.request_id,
                "guild_id": self.guild_id,
                "reviewed_by_discord_user_id": self.reviewed_by_discord_user_id,
                "expected_request_fingerprint": self.expected_request_fingerprint,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class ApprovedAccountRegistration:
    """Committed approval receipt detached from persistence."""

    request_id: int
    requester_discord_user_id: str
    persona_id: str
    persona_display_name: str
    persona_status: PersonaStatus
    game_account_id: int
    game_region: GameRegion
    uma_pid: str
    nickname: str
    affiliation: str | None
    wallet_balance: int
    initial_grant_amount: int
    resolved_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(
            self,
            "requester_discord_user_id",
            _text(
                self.requester_discord_user_id,
                field_name="requester_discord_user_id",
                max_length=32,
            ),
        )
        object.__setattr__(self, "persona_id", _persona_id(self.persona_id))
        object.__setattr__(
            self,
            "persona_display_name",
            _text(self.persona_display_name, field_name="persona_display_name", max_length=100),
        )
        object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        if self.persona_status is not PersonaStatus.NORMAL:
            raise ValueError("Approved registration Persona must start normal.")
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        for field_name, max_length, optional in (
            ("uma_pid", 32, False),
            ("nickname", 100, False),
            ("affiliation", 100, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )
        object.__setattr__(
            self,
            "wallet_balance",
            _non_negative_int(self.wallet_balance, field_name="wallet_balance"),
        )
        object.__setattr__(
            self,
            "initial_grant_amount",
            _positive_int(self.initial_grant_amount, field_name="initial_grant_amount"),
        )
        if self.wallet_balance != self.initial_grant_amount:
            raise ValueError("Fresh approval wallet must contain exactly the initial grant.")
        object.__setattr__(
            self,
            "resolved_at",
            normalize_utc_datetime(self.resolved_at, field_name="resolved_at"),
        )
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION,
            "request_id": self.request_id,
            "requester_discord_user_id": self.requester_discord_user_id,
            "request_status": RegistrationRequestStatus.APPROVED.value,
            "persona_id": self.persona_id,
            "persona_display_name": self.persona_display_name,
            "persona_status": self.persona_status.value,
            "game_account_id": self.game_account_id,
            "game_region": self.game_region.value,
            "uma_pid": self.uma_pid,
            "nickname": self.nickname,
            "affiliation": self.affiliation,
            "wallet_balance": self.wallet_balance,
            "initial_grant_amount": self.initial_grant_amount,
            "point_action": ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION,
            "resolved_at": self.resolved_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> ApprovedAccountRegistration:
        if payload.get("schema_version") != ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported registration approval audit schema version.")
        if (
            payload.get("request_status") != RegistrationRequestStatus.APPROVED.value
            or payload.get("point_action") != ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION
        ):
            raise ValueError("Registration approval audit payload has invalid canonical status.")
        return cls(
            request_id=_positive_int(payload["request_id"], field_name="request_id"),
            requester_discord_user_id=_payload_text(payload, "requester_discord_user_id"),
            persona_id=_payload_text(payload, "persona_id"),
            persona_display_name=_payload_text(payload, "persona_display_name"),
            persona_status=PersonaStatus(_payload_text(payload, "persona_status")),
            game_account_id=_positive_int(payload["game_account_id"], field_name="game_account_id"),
            game_region=GameRegion(_payload_text(payload, "game_region")),
            uma_pid=_payload_text(payload, "uma_pid"),
            nickname=_payload_text(payload, "nickname"),
            affiliation=_payload_optional_text(payload, "affiliation"),
            wallet_balance=_non_negative_int(payload["wallet_balance"], field_name="wallet_balance"),
            initial_grant_amount=_positive_int(
                payload["initial_grant_amount"],
                field_name="initial_grant_amount",
            ),
            resolved_at=datetime.fromisoformat(_payload_text(payload, "resolved_at")),
        )


@dataclass(frozen=True, slots=True)
class RejectedAccountRegistration:
    """Committed rejection receipt detached from persistence."""

    request_id: int
    requester_discord_user_id: str
    reason: str
    resolved_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(
            self,
            "requester_discord_user_id",
            _text(
                self.requester_discord_user_id,
                field_name="requester_discord_user_id",
                max_length=32,
            ),
        )
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        object.__setattr__(
            self,
            "resolved_at",
            normalize_utc_datetime(self.resolved_at, field_name="resolved_at"),
        )
        if not isinstance(self.exact_retry, bool):
            raise ValueError("exact_retry must be a boolean.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION,
            "request_id": self.request_id,
            "requester_discord_user_id": self.requester_discord_user_id,
            "request_status": RegistrationRequestStatus.CANCELLED.value,
            "reason": self.reason,
            "resolved_at": self.resolved_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> RejectedAccountRegistration:
        if payload.get("schema_version") != ACCOUNT_REGISTRATION_REVIEW_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported registration rejection audit schema version.")
        if payload.get("request_status") != RegistrationRequestStatus.CANCELLED.value:
            raise ValueError("Registration rejection audit payload has invalid canonical status.")
        return cls(
            request_id=_positive_int(payload["request_id"], field_name="request_id"),
            requester_discord_user_id=_payload_text(payload, "requester_discord_user_id"),
            reason=_payload_text(payload, "reason"),
            resolved_at=datetime.fromisoformat(_payload_text(payload, "resolved_at")),
        )


class StaffRegistrationReviewQueryRepository(Protocol):
    """Read-only staff Persona panel and pending-request projections."""

    def get_panel(self, *, guild_id: str, persona_id: str | None) -> StaffPersonaPanel: ...

    def search_personas(self, *, query: str, limit: int) -> tuple[StaffPersonaChoice, ...]: ...

    def list_pending_requests(
        self,
        *,
        guild_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> StaffRegistrationRequestPage: ...

    def get_pending_request(
        self,
        *,
        guild_id: str,
        request_id: int,
    ) -> StaffRegistrationRequestState | None: ...


class StaffRegistrationReviewQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def staff_registration_review(self) -> StaffRegistrationReviewQueryRepository: ...


@dataclass(frozen=True, slots=True)
class StaffRegistrationReviewQueries:
    """Application entry point for fresh staff review projections."""

    query_runner: QueryRunner[StaffRegistrationReviewQueryUnitOfWork]

    def get_panel(self, *, guild_id: str, persona_id: str | None = None) -> StaffPersonaPanel:
        normalized_guild = _text(guild_id, field_name="guild_id", max_length=32)
        normalized_persona = None if persona_id is None else _persona_id(persona_id)
        assert normalized_guild is not None
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.staff_registration_review.get_panel(
                guild_id=normalized_guild,
                persona_id=normalized_persona,
            )
        )

    def search_personas(self, *, query: str, limit: int = 25) -> tuple[StaffPersonaChoice, ...]:
        normalized_query = _text(query, field_name="query", max_length=100, optional=True) or ""
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.staff_registration_review.search_personas(
                query=normalized_query,
                limit=limit,
            )
        )

    def list_pending_requests(self, *, guild_id: str, page: int) -> StaffRegistrationRequestPage:
        normalized_guild = _text(guild_id, field_name="guild_id", max_length=32)
        normalized_page = _non_negative_int(page, field_name="page")
        assert normalized_guild is not None
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.staff_registration_review.list_pending_requests(
                guild_id=normalized_guild,
                page=normalized_page,
                offset=normalized_page * STAFF_REGISTRATION_REQUEST_PAGE_SIZE,
                limit=STAFF_REGISTRATION_REQUEST_PAGE_SIZE,
            )
        )

    def get_pending_request(self, *, guild_id: str, request_id: int) -> StaffRegistrationRequestState:
        normalized_guild = _text(guild_id, field_name="guild_id", max_length=32)
        normalized_request_id = _positive_int(request_id, field_name="request_id")
        assert normalized_guild is not None
        result = self.query_runner.run(
            lambda unit_of_work: unit_of_work.staff_registration_review.get_pending_request(
                guild_id=normalized_guild,
                request_id=normalized_request_id,
            )
        )
        if result is None:
            raise StaffRegistrationRequestNotFoundError("Pending registration request was not found.")
        return result


class RegistrationReviewRepository(RegistrationBootstrapRepository, Protocol):
    """Mutation persistence used under one requester-first transaction."""

    def find_request_locator(self, *, request_id: int) -> RegistrationRequestLocator | None: ...

    def lock_requester(self, *, discord_user_id: str) -> RegistrationReviewRequester | None: ...

    def lock_request(self, *, request_id: int) -> StaffRegistrationRequestState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredRegistrationReviewOperation | None: ...

    def registered_game_account_exists(self, *, game_region: GameRegion, uma_pid: str) -> bool: ...

    def resolve_approved_request(
        self,
        *,
        command: ApproveAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> None: ...

    def reject_request(
        self,
        *,
        command: RejectAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> RejectedAccountRegistration: ...


class RegistrationReviewUnitOfWork(UnitOfWork, Protocol):
    @property
    def registration_review(self) -> RegistrationReviewRepository: ...


@dataclass(frozen=True, slots=True)
class RegistrationReviewCommands:
    """Approve or reject exactly one current pending request."""

    command_runner: CommandRunner[RegistrationReviewUnitOfWork]
    clock: Callable[[], datetime]
    persona_id_factory: Callable[[], str]
    bootstrap_service: AccountRegistrationBootstrapService = AccountRegistrationBootstrapService()

    def approve(self, command: ApproveAccountRegistrationRequest) -> ApprovedAccountRegistration:
        return self.command_runner.run(lambda uow: self._approve(uow.registration_review, command))

    def reject(self, command: RejectAccountRegistrationRequest) -> RejectedAccountRegistration:
        return self.command_runner.run(lambda uow: self._reject(uow.registration_review, command))

    def _approve(
        self,
        repository: RegistrationReviewRepository,
        command: ApproveAccountRegistrationRequest,
    ) -> ApprovedAccountRegistration:
        request, requester, stored = self._lock_request_authority(
            repository=repository,
            request_id=command.request_id,
            guild_id=command.guild_id,
            idempotency_key=command.idempotency_key,
        )
        if stored is not None:
            return self._resolve_approval_retry(stored=stored, command=command)
        self._validate_pending_request(
            request=request,
            expected_fingerprint=command.expected_request_fingerprint,
        )
        if requester.persona_id is not None:
            raise StaffRegistrationRequesterLinkedError("Registration requester is already linked to a Persona.")
        if repository.registered_game_account_exists(
            game_region=request.game_region,
            uma_pid=request.uma_pid,
        ):
            raise StaffRegistrationPidUnavailableError("Registration PID is already registered.")
        resolved_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        persona_id = _persona_id(self.persona_id_factory())
        bootstrap_command = CreateAccountRegistrationBootstrap(
            guild_id=command.guild_id,
            actor_discord_user_id=command.reviewed_by_discord_user_id,
            target_discord_user_id=request.requester_discord_user_id,
            persona_id=persona_id,
            persona_display_name=request.discord_display_name_snapshot,
            game_region=request.game_region,
            uma_pid=request.uma_pid,
            nickname=request.nickname,
            affiliation=request.affiliation,
            audit_type=AccountRegistrationAuditType.APPROVED,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            registered_at=resolved_at,
            correlation_id=command.correlation_id,
            reason=command.review_note,
        )
        try:
            result = self.bootstrap_service.register(
                repository,
                bootstrap_command,
                before_data=request.to_audit_payload(),
                receipt_factory=lambda bootstrap: ApprovedAccountRegistration(
                    request_id=request.request_id,
                    requester_discord_user_id=bootstrap.target_discord_user_id,
                    persona_id=bootstrap.persona_id,
                    persona_display_name=bootstrap.persona_display_name,
                    persona_status=bootstrap.persona_status,
                    game_account_id=bootstrap.game_account_id,
                    game_region=bootstrap.game_region,
                    uma_pid=bootstrap.uma_pid,
                    nickname=bootstrap.nickname,
                    affiliation=bootstrap.affiliation,
                    wallet_balance=bootstrap.wallet_balance,
                    initial_grant_amount=bootstrap.initial_grant_amount,
                    resolved_at=bootstrap.registered_at,
                ),
            )
        except RegistrationBootstrapPidUnavailableError as error:
            raise StaffRegistrationPidUnavailableError(str(error)) from error
        except RegistrationBootstrapIdempotencyConflictError as error:
            raise StaffRegistrationReviewIdempotencyConflictError(str(error)) from error
        except RegistrationBootstrapConcurrentConflictError as error:
            raise StaffRegistrationReviewConcurrentConflictError(str(error)) from error
        except RegistrationBootstrapAuditError as error:
            raise StaffRegistrationReviewAuditError(str(error)) from error
        repository.resolve_approved_request(
            command=command,
            request=request,
            requester=requester,
            resolved_at=resolved_at,
        )
        self._verify_approval(
            result=result,
            request=request,
            persona_id=persona_id,
            resolved_at=resolved_at,
        )
        return result

    def _reject(
        self,
        repository: RegistrationReviewRepository,
        command: RejectAccountRegistrationRequest,
    ) -> RejectedAccountRegistration:
        request, requester, stored = self._lock_request_authority(
            repository=repository,
            request_id=command.request_id,
            guild_id=command.guild_id,
            idempotency_key=command.idempotency_key,
        )
        if stored is not None:
            return self._resolve_rejection_retry(stored=stored, command=command)
        self._validate_pending_request(
            request=request,
            expected_fingerprint=command.expected_request_fingerprint,
        )
        resolved_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        result = repository.reject_request(
            command=command,
            request=request,
            requester=requester,
            resolved_at=resolved_at,
        )
        if (
            result.request_id != request.request_id
            or result.requester_discord_user_id != request.requester_discord_user_id
            or result.reason != command.reason
            or result.resolved_at != resolved_at
            or result.exact_retry
        ):
            raise StaffRegistrationReviewAuditError("Rejected registration receipt is inconsistent.")
        return result

    @staticmethod
    def _lock_request_authority(
        *,
        repository: RegistrationReviewRepository,
        request_id: int,
        guild_id: str,
        idempotency_key: str,
    ) -> tuple[
        StaffRegistrationRequestState,
        RegistrationReviewRequester,
        StoredRegistrationReviewOperation | None,
    ]:
        locator = repository.find_request_locator(request_id=request_id)
        if locator is None or locator.guild_id != guild_id:
            raise StaffRegistrationRequestNotFoundError("Registration request was not found in this guild.")

        # Requester is the first lock root shared with request submission. The
        # request and absent-operation lookup follow it to avoid lock inversion
        # and InnoDB gap-lock deadlocks.
        requester = repository.lock_requester(discord_user_id=locator.requester_discord_user_id)
        if requester is None:
            raise StaffRegistrationReviewAuditError("Registration request has no Discord requester authority.")
        request = repository.lock_request(request_id=request_id)
        if request is None:
            raise StaffRegistrationRequestNotFoundError("Registration request no longer exists.")
        if (
            request.guild_id != locator.guild_id
            or request.requester_discord_user_id != locator.requester_discord_user_id
            or request.discord_account_id != requester.discord_account_id
            or requester.discord_user_id != locator.requester_discord_user_id
        ):
            raise StaffRegistrationReviewAuditError("Registration request authority changed during locking.")
        stored = repository.find_operation(idempotency_key=idempotency_key)
        return request, requester, stored

    @staticmethod
    def _validate_pending_request(
        *,
        request: StaffRegistrationRequestState,
        expected_fingerprint: str,
    ) -> None:
        if request.status is not RegistrationRequestStatus.PENDING or request.active_marker is not True:
            raise StaffRegistrationRequestNotPendingError("Registration request is no longer pending.")
        if request.state_fingerprint != expected_fingerprint:
            raise StaffRegistrationRequestStaleError("Registration request changed after Preview.")

    @staticmethod
    def _verify_approval(
        *,
        result: ApprovedAccountRegistration,
        request: StaffRegistrationRequestState,
        persona_id: str,
        resolved_at: datetime,
    ) -> None:
        if (
            result.request_id != request.request_id
            or result.requester_discord_user_id != request.requester_discord_user_id
            or result.persona_id != persona_id
            or result.persona_display_name != request.discord_display_name_snapshot
            or result.persona_status is not PersonaStatus.NORMAL
            or result.game_region is not request.game_region
            or result.uma_pid != request.uma_pid
            or result.nickname != request.nickname
            or result.affiliation != request.affiliation
            or result.wallet_balance != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or result.initial_grant_amount != ACCOUNT_REGISTRATION_INITIAL_GRANT
            or result.resolved_at != resolved_at
            or result.exact_retry
        ):
            raise StaffRegistrationReviewAuditError("Approved registration receipt is inconsistent.")

    @staticmethod
    def _resolve_approval_retry(
        *,
        stored: StoredRegistrationReviewOperation,
        command: ApproveAccountRegistrationRequest,
    ) -> ApprovedAccountRegistration:
        if (
            stored.type != AccountRegistrationAuditType.APPROVED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffRegistrationReviewIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffRegistrationReviewAuditError("Approval exact-retry operation has no receipt payload.")
        try:
            result = ApprovedAccountRegistration.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffRegistrationReviewAuditError("Approval exact-retry receipt is malformed.") from error
        if (
            result.request_id != command.request_id
            or stored.persona_id != result.persona_id
            or stored.game_account_id != result.game_account_id
            or stored.discord_user_id != result.requester_discord_user_id
        ):
            raise StaffRegistrationReviewAuditError("Approval exact-retry operation context is malformed.")
        return replace(result, exact_retry=True)

    @staticmethod
    def _resolve_rejection_retry(
        *,
        stored: StoredRegistrationReviewOperation,
        command: RejectAccountRegistrationRequest,
    ) -> RejectedAccountRegistration:
        if (
            stored.type != AccountRegistrationAuditType.REJECTED.value
            or stored.request_fingerprint != command.request_fingerprint
        ):
            raise StaffRegistrationReviewIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise StaffRegistrationReviewAuditError("Rejection exact-retry operation has no receipt payload.")
        try:
            result = RejectedAccountRegistration.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as error:
            raise StaffRegistrationReviewAuditError("Rejection exact-retry receipt is malformed.") from error
        if (
            result.request_id != command.request_id
            or stored.persona_id is not None
            or stored.game_account_id is not None
            or stored.discord_user_id != result.requester_discord_user_id
        ):
            raise StaffRegistrationReviewAuditError("Rejection exact-retry operation context is malformed.")
        return replace(result, exact_retry=True)
