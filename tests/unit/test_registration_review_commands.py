"""Staff registration-review Application command and query contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT,
    AccountRegistrationBootstrap,
    ApproveAccountRegistrationRequest,
    CreateAccountRegistrationBootstrap,
    RegistrationRequestLocator,
    RegistrationReviewCommands,
    RegistrationReviewRequester,
    RejectAccountRegistrationRequest,
    RejectedAccountRegistration,
    StaffPersonaChoice,
    StaffPersonaPanel,
    StaffRegistrationPidUnavailableError,
    StaffRegistrationRequesterLinkedError,
    StaffRegistrationRequestNotPendingError,
    StaffRegistrationRequestPage,
    StaffRegistrationRequestStaleError,
    StaffRegistrationRequestState,
    StaffRegistrationReviewIdempotencyConflictError,
    StaffRegistrationReviewQueries,
    StoredRegistrationReviewOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus

NOW = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _pending(**changes: object) -> StaffRegistrationRequestState:
    values: dict[str, object] = {
        "request_id": 51,
        "discord_account_id": 7,
        "guild_id": "987",
        "requester_discord_user_id": "123",
        "discord_display_name_snapshot": "Discord 표시명",
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "nickname": "게임 닉네임",
        "affiliation": "소속",
        "status": RegistrationRequestStatus.PENDING,
        "active_marker": True,
        "reason": None,
        "created_at": datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        "resolved_at": None,
    }
    values.update(changes)
    return StaffRegistrationRequestState(**values)  # type: ignore[arg-type]


def _approve_command(request: StaffRegistrationRequestState, **changes: object) -> ApproveAccountRegistrationRequest:
    values: dict[str, object] = {
        "request_id": request.request_id,
        "guild_id": request.guild_id,
        "reviewed_by_discord_user_id": "900",
        "expected_request_fingerprint": request.state_fingerprint,
        "idempotency_key": "approve-1",
        "correlation_id": "approve-1",
        "review_note": None,
    }
    values.update(changes)
    return ApproveAccountRegistrationRequest(**values)  # type: ignore[arg-type]


def _reject_command(request: StaffRegistrationRequestState, **changes: object) -> RejectAccountRegistrationRequest:
    values: dict[str, object] = {
        "request_id": request.request_id,
        "guild_id": request.guild_id,
        "reviewed_by_discord_user_id": "900",
        "expected_request_fingerprint": request.state_fingerprint,
        "reason": "PID 확인 불가",
        "idempotency_key": "reject-1",
        "correlation_id": "reject-1",
    }
    values.update(changes)
    return RejectAccountRegistrationRequest(**values)  # type: ignore[arg-type]


class RecordingReviewRepository:
    def __init__(self, request: StaffRegistrationRequestState | None = None) -> None:
        self.request = request or _pending()
        self.requester = RegistrationReviewRequester(
            discord_account_id=self.request.discord_account_id,
            discord_user_id=self.request.requester_discord_user_id,
            persona_id=None,
        )
        self.operation: StoredRegistrationReviewOperation | None = None
        self.pid_exists = False
        self.approvals = 0
        self.rejections = 0
        self.calls: list[str] = []

    def find_request_locator(self, *, request_id: int) -> RegistrationRequestLocator | None:
        self.calls.append("locator")
        if request_id != self.request.request_id:
            return None
        return RegistrationRequestLocator(
            request_id=request_id,
            guild_id=self.request.guild_id,
            requester_discord_user_id=self.request.requester_discord_user_id,
        )

    def lock_requester(self, **_kwargs: object) -> RegistrationReviewRequester | None:
        self.calls.append("requester")
        return self.requester

    def lock_request(self, **_kwargs: object) -> StaffRegistrationRequestState | None:
        self.calls.append("request")
        return self.request

    def find_operation(self, **_kwargs: object) -> StoredRegistrationReviewOperation | None:
        self.calls.append("operation")
        return self.operation

    def registered_game_account_exists(self, **_kwargs: object) -> bool:
        self.calls.append("pid")
        return self.pid_exists

    def create_registration_bootstrap(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
    ) -> AccountRegistrationBootstrap:
        self.calls.append("bootstrap")
        self.approvals += 1
        self.requester = replace(self.requester, persona_id=command.persona_id)
        return AccountRegistrationBootstrap(
            operation_id=71,
            target_discord_user_id=command.target_discord_user_id,
            persona_id=command.persona_id,
            persona_display_name=command.persona_display_name,
            persona_status=PersonaStatus.NORMAL,
            game_account_id=81,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            wallet_balance=ACCOUNT_REGISTRATION_INITIAL_GRANT,
            initial_grant_amount=ACCOUNT_REGISTRATION_INITIAL_GRANT,
            registered_at=command.registered_at,
        )

    def complete_registration_bootstrap_audit(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
        bootstrap: AccountRegistrationBootstrap,
        before_data: Mapping[str, object] | None,
        after_data: Mapping[str, object],
    ) -> None:
        self.calls.append("audit")
        self.operation = StoredRegistrationReviewOperation(
            request_fingerprint=command.request_fingerprint,
            type=command.audit_type.value,
            persona_id=bootstrap.persona_id,
            game_account_id=bootstrap.game_account_id,
            discord_user_id=bootstrap.target_discord_user_id,
            after_data=after_data,
        )

    def resolve_approved_request(
        self,
        *,
        command: ApproveAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> None:
        self.calls.append("resolve")
        self.request = replace(
            request,
            status=RegistrationRequestStatus.APPROVED,
            active_marker=None,
            reason=command.review_note,
            resolved_at=resolved_at,
        )

    def reject_request(
        self,
        *,
        command: RejectAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> RejectedAccountRegistration:
        self.calls.append("reject")
        self.rejections += 1
        result = RejectedAccountRegistration(
            request_id=request.request_id,
            requester_discord_user_id=request.requester_discord_user_id,
            reason=command.reason,
            resolved_at=resolved_at,
        )
        self.operation = StoredRegistrationReviewOperation(
            request_fingerprint=command.request_fingerprint,
            type="account_registration_rejected",
            persona_id=None,
            game_account_id=None,
            discord_user_id=result.requester_discord_user_id,
            after_data=result.to_audit_payload(),
        )
        self.request = replace(
            request,
            status=RegistrationRequestStatus.CANCELLED,
            active_marker=None,
            reason=command.reason,
            resolved_at=resolved_at,
        )
        return result


class ReviewUnitOfWork:
    def __init__(self, repository: RecordingReviewRepository) -> None:
        self.registration_review = repository

    def __enter__(self) -> ReviewUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingReviewRepository) -> RegistrationReviewCommands:
    return RegistrationReviewCommands(
        CommandRunner(lambda: ReviewUnitOfWork(repository)),
        clock=lambda: NOW,
        persona_id_factory=lambda: PERSONA_ID,
    )


def test_approval_uses_requester_first_authority_and_exact_retry_receipt() -> None:
    repository = RecordingReviewRepository()
    commands = _commands(repository)
    command = _approve_command(repository.request)

    created = commands.approve(command)
    retried = commands.approve(command)

    assert created.persona_id == PERSONA_ID
    assert created.wallet_balance == created.initial_grant_amount == 500
    assert created.exact_retry is False
    assert retried == replace(created, exact_retry=True)
    assert repository.approvals == 1
    assert repository.calls[:8] == [
        "locator",
        "requester",
        "request",
        "operation",
        "pid",
        "bootstrap",
        "audit",
        "resolve",
    ]
    assert repository.calls[8:12] == ["locator", "requester", "request", "operation"]


def test_approval_rejects_stale_linked_and_registered_pid_without_write() -> None:
    repository = RecordingReviewRepository()
    commands = _commands(repository)
    with pytest.raises(StaffRegistrationRequestStaleError):
        commands.approve(_approve_command(repository.request, expected_request_fingerprint="0" * 64))

    repository.requester = replace(repository.requester, persona_id="existing-persona")
    with pytest.raises(StaffRegistrationRequesterLinkedError):
        commands.approve(_approve_command(repository.request))

    repository.requester = replace(repository.requester, persona_id=None)
    repository.pid_exists = True
    with pytest.raises(StaffRegistrationPidUnavailableError):
        commands.approve(_approve_command(repository.request))

    assert repository.approvals == 0


def test_approval_changed_payload_same_key_conflicts_after_commit() -> None:
    repository = RecordingReviewRepository()
    commands = _commands(repository)
    commands.approve(_approve_command(repository.request))

    with pytest.raises(StaffRegistrationReviewIdempotencyConflictError):
        commands.approve(_approve_command(_pending(), review_note="다른 검토 메모"))

    assert repository.approvals == 1


def test_rejection_requires_reason_and_writes_no_approval_bootstrap() -> None:
    repository = RecordingReviewRepository()
    commands = _commands(repository)
    command = _reject_command(repository.request)

    rejected = commands.reject(command)
    retried = commands.reject(command)

    assert rejected.reason == "PID 확인 불가"
    assert retried == replace(rejected, exact_retry=True)
    assert repository.rejections == 1
    assert repository.approvals == 0
    with pytest.raises(ValueError, match="reason"):
        _reject_command(_pending(), reason="   ")


def test_terminal_request_with_new_key_is_not_replayed() -> None:
    repository = RecordingReviewRepository()
    commands = _commands(repository)
    commands.reject(_reject_command(repository.request))
    repository.operation = None

    with pytest.raises(StaffRegistrationRequestNotPendingError):
        commands.reject(_reject_command(_pending(), idempotency_key="reject-2"))


class RecordingReviewQueryRepository:
    def __init__(self) -> None:
        self.list_call: dict[str, object] = {}

    def get_panel(self, **_kwargs: object) -> StaffPersonaPanel:
        return StaffPersonaPanel(pending_request_count=1, selected_persona=None)

    def search_personas(self, **_kwargs: object) -> tuple[StaffPersonaChoice, ...]:
        return ()

    def list_pending_requests(self, **kwargs: object) -> StaffRegistrationRequestPage:
        self.list_call = kwargs
        return StaffRegistrationRequestPage(page=int(kwargs["page"]), total_count=0, items=())

    def get_pending_request(self, **_kwargs: object) -> StaffRegistrationRequestState | None:
        return _pending()


class ReviewQueryUnitOfWork:
    def __init__(self, repository: RecordingReviewQueryRepository) -> None:
        self.staff_registration_review = repository

    def __enter__(self) -> ReviewQueryUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_query_boundary_owns_ten_row_page_offset() -> None:
    repository = RecordingReviewQueryRepository()
    queries = StaffRegistrationReviewQueries(QueryRunner(lambda: ReviewQueryUnitOfWork(repository)))

    page = queries.list_pending_requests(guild_id="987", page=2)

    assert page.page == 2
    assert repository.list_call == {
        "guild_id": "987",
        "page": 2,
        "offset": 20,
        "limit": 10,
    }
