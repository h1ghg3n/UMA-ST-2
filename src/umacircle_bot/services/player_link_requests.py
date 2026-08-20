from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    IdentityBackfillTask,
    Persona,
    PlayerLinkOperationAudit,
    PlayerLinkRequest,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import (
    AccountAlreadyRegisteredError,
    PlayerLinkRequestConflictError,
    PlayerLinkRequestError,
)
from umacircle_bot.domain.identity import IdentityStatus, validate_registration_pid
from umacircle_bot.domain.player_link import (
    normalize_player_name_relaxed,
    normalize_player_name_strict,
    player_name_similarity,
)
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import (
    PlayerLinkApprovalDTO,
    PlayerLinkCandidateDTO,
    PlayerLinkMutationDTO,
    PlayerLinkRequestDTO,
)
from umacircle_bot.services.legacy_import import (
    LEGACY_IDENTITY_POINT_IMPORT_KIND,
    LEGACY_IDENTITY_POINT_RECORD_TYPE,
)

PLAYER_LINK_SUBMIT_ACTION = "submit"
PLAYER_LINK_REVISE_ACTION = "revise"
PLAYER_LINK_CANCEL_ACTION = "cancel"
PLAYER_LINK_REVIEW_ACTION = "review"
PLAYER_LINK_APPROVE_ACTION = "approve"
PLAYER_LINK_REJECT_ACTION = "reject"
ACTIVE_PLAYER_LINK_STATUSES = frozenset({"pending", "review_required"})
_CANDIDATE_IDENTITY_STATUSES = frozenset({IdentityStatus.PENDING.value, IdentityStatus.CONFLICT.value})
_CANDIDATE_TASK_STATUSES = frozenset({"pending", "conflict"})


@dataclass(frozen=True, slots=True)
class SubmitPlayerLinkRequestCommand:
    guild_id: str
    requester_discord_user_id: str
    discord_nickname_snapshot: str
    submitted_ingame_name: str
    submitted_uma_pid: str
    idempotency_key: str
    submitted_nickname_chunk: str | None = None
    submitted_participation_hint: str | None = None
    requester_note: str | None = None
    submitted_by_discord_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerLinkCandidateSearchTerms:
    submitted_ingame_name: str
    discord_nickname_snapshot: str
    submitted_nickname_chunk: str | None = None


@dataclass(frozen=True, slots=True)
class CancelPlayerLinkRequestCommand:
    request_id: int
    guild_id: str
    requester_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RevisePlayerLinkRequestCommand:
    request_id: int
    guild_id: str
    requester_discord_user_id: str
    discord_nickname_snapshot: str
    submitted_ingame_name: str
    submitted_uma_pid: str
    idempotency_key: str
    submitted_nickname_chunk: str | None = None
    submitted_participation_hint: str | None = None
    requester_note: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovePlayerLinkRequestCommand:
    request_id: int
    selected_game_account_id: int
    expected_candidate_fingerprint: str
    reviewed_by_discord_user_id: str
    idempotency_key: str
    review_note: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPlayerLinkRequestCommand:
    request_id: int
    reviewed_by_discord_user_id: str
    idempotency_key: str
    review_note: str


@dataclass(frozen=True, slots=True)
class RejectPlayerLinkRequestCommand:
    request_id: int
    reviewed_by_discord_user_id: str
    idempotency_key: str
    review_note: str


def submit_player_link_request(
    session: Session,
    *,
    command: SubmitPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    values = _normalize_submit_command(command)
    submitted_by_discord_user_id = _optional_submitter_id(command.submitted_by_discord_user_id, values)
    fingerprint = _fingerprint(
        {
            "action": PLAYER_LINK_SUBMIT_ACTION,
            **values,
            "submitted_by_discord_user_id": submitted_by_discord_user_id,
        }
    )
    _begin_sqlite_outer_transaction_if_needed(session)

    try:
        with session.begin_nested():
            audit = _load_audit(session, values["idempotency_key"])
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_SUBMIT_ACTION, fingerprint)

            _raise_if_requester_already_registered(session, values["requester_discord_user_id"])
            active_request = _load_active_request(
                session,
                guild_id=values["guild_id"],
                requester_discord_user_id=values["requester_discord_user_id"],
                lock=False,
            )
            if active_request is not None:
                raise PlayerLinkRequestConflictError("an active player-link request already exists")

            request = PlayerLinkRequest(
                **values,
                request_fingerprint=fingerprint,
                status="pending",
                active_request_marker=1,
            )
            session.add(request)
            session.flush()
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_SUBMIT_ACTION,
                actor_discord_user_id=submitted_by_discord_user_id,
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
                before_json=None,
                after_json=_request_json(request),
                reason=None,
            )
            session.add(audit)
            session.flush()
            return PlayerLinkMutationDTO(
                action=PLAYER_LINK_SUBMIT_ACTION,
                audit_id=audit.id,
                request=_request_dto(request),
            )
    except IntegrityError as exc:
        return _recover_submit_integrity_conflict(
            session,
            error=exc,
            values=values,
            fingerprint=fingerprint,
        )


def get_owned_player_link_request(
    session: Session,
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    normalized_guild_id = _discord_id(guild_id, field="guild ID")
    requester = _discord_id(requester_discord_user_id, field="requester Discord user ID")
    request = session.scalar(
        select(PlayerLinkRequest)
        .where(
            PlayerLinkRequest.guild_id == normalized_guild_id,
            PlayerLinkRequest.requester_discord_user_id == requester,
        )
        .order_by(PlayerLinkRequest.id.desc())
        .limit(1)
    )
    return _request_dto(request) if request is not None else None


def revise_player_link_request(
    session: Session,
    *,
    command: RevisePlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    values = _normalize_revise_command(command)
    fingerprint = _fingerprint({"action": PLAYER_LINK_REVISE_ACTION, **values})
    _begin_sqlite_outer_transaction_if_needed(session)
    try:
        with session.begin_nested():
            audit = _load_audit(session, str(values["idempotency_key"]))
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REVISE_ACTION, fingerprint)
            request = _load_request_for_update(session, int(values["request_id"]))
            if (
                request is None
                or request.guild_id != values["guild_id"]
                or request.requester_discord_user_id != values["requester_discord_user_id"]
                or request.status not in ACTIVE_PLAYER_LINK_STATUSES
            ):
                raise PlayerLinkRequestConflictError("player-link request is no longer available for revision")
            before = _request_json(request)
            request.discord_nickname_snapshot = str(values["discord_nickname_snapshot"])
            request.submitted_ingame_name = str(values["submitted_ingame_name"])
            request.submitted_uma_pid = str(values["submitted_uma_pid"])
            request.submitted_nickname_chunk = values["submitted_nickname_chunk"]
            request.submitted_participation_hint = values["submitted_participation_hint"]
            request.requester_note = values["requester_note"]
            request.status = "pending"
            request.reviewed_by_discord_user_id = None
            request.review_note = None
            request.resolved_at = None
            session.flush()
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_REVISE_ACTION,
                actor_discord_user_id=str(values["requester_discord_user_id"]),
                idempotency_key=str(values["idempotency_key"]),
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=_request_json(request),
                reason=None,
            )
            session.add(audit)
            session.flush()
            return PlayerLinkMutationDTO(
                action=PLAYER_LINK_REVISE_ACTION, audit_id=audit.id, request=_request_dto(request)
            )
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, str(values["idempotency_key"]), lock=True)
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REVISE_ACTION, fingerprint)
        raise PlayerLinkRequestConflictError("player-link request revision conflicted") from exc


def list_player_link_requests(
    session: Session,
    *,
    guild_id: str,
    statuses: tuple[str, ...] = ("pending", "review_required"),
    limit: int = 25,
) -> tuple[PlayerLinkRequestDTO, ...]:
    normalized_guild_id = _discord_id(guild_id, field="guild ID")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
        raise PlayerLinkRequestError("player-link request limit must be between 1 and 25")
    allowed_statuses = frozenset(statuses)
    if not allowed_statuses or not allowed_statuses.issubset(ACTIVE_PLAYER_LINK_STATUSES):
        raise PlayerLinkRequestError("player-link request statuses must be active statuses")
    requests = session.scalars(
        select(PlayerLinkRequest)
        .where(
            PlayerLinkRequest.guild_id == normalized_guild_id,
            PlayerLinkRequest.status.in_(allowed_statuses),
        )
        .order_by(PlayerLinkRequest.created_at, PlayerLinkRequest.id)
        .limit(limit)
    ).all()
    return tuple(_request_dto(request) for request in requests)


def get_player_link_request_for_staff(
    session: Session,
    *,
    guild_id: str,
    request_id: int,
) -> PlayerLinkRequestDTO:
    normalized_guild_id = _discord_id(guild_id, field="guild ID")
    normalized_request_id = _positive_id(request_id, field="player-link request ID")
    request = session.scalar(
        select(PlayerLinkRequest).where(
            PlayerLinkRequest.id == normalized_request_id,
            PlayerLinkRequest.guild_id == normalized_guild_id,
        )
    )
    if request is None:
        raise PlayerLinkRequestConflictError("player-link request does not exist in this guild")
    return _request_dto(request)


def cancel_player_link_request(
    session: Session,
    *,
    command: CancelPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    request_id = _positive_id(command.request_id, field="player-link request ID")
    guild_id = _discord_id(command.guild_id, field="guild ID")
    requester = _discord_id(command.requester_discord_user_id, field="requester Discord user ID")
    key = _required_text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="cancellation reason", maximum=255)
    fingerprint = _fingerprint(
        {
            "action": PLAYER_LINK_CANCEL_ACTION,
            "request_id": request_id,
            "guild_id": guild_id,
            "requester_discord_user_id": requester,
            "reason": reason,
        }
    )
    _begin_sqlite_outer_transaction_if_needed(session)

    try:
        with session.begin_nested():
            audit = _load_audit(session, key)
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_CANCEL_ACTION, fingerprint)

            request = session.scalar(
                select(PlayerLinkRequest)
                .where(PlayerLinkRequest.id == request_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if request is None or request.guild_id != guild_id or request.requester_discord_user_id != requester:
                raise PlayerLinkRequestConflictError("player-link request is not owned by the requester")
            if request.status not in ACTIVE_PLAYER_LINK_STATUSES:
                raise PlayerLinkRequestConflictError("player-link request is no longer active")

            before = _request_json(request)
            request.status = "cancelled"
            request.active_request_marker = None
            request.review_note = None
            session.flush()
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_CANCEL_ACTION,
                actor_discord_user_id=requester,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=_request_json(request),
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return PlayerLinkMutationDTO(
                action=PLAYER_LINK_CANCEL_ACTION,
                audit_id=audit.id,
                request=_request_dto(request),
            )
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, key, lock=True)
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_CANCEL_ACTION, fingerprint)
        raise PlayerLinkRequestConflictError("player-link request cancellation conflicted") from exc


def approve_player_link_request(
    session: Session,
    *,
    command: ApprovePlayerLinkRequestCommand,
) -> PlayerLinkApprovalDTO | PlayerLinkMutationDTO:
    values = _normalize_approval_command(command)
    fingerprint = _fingerprint({"action": PLAYER_LINK_APPROVE_ACTION, **values})
    review_fingerprint = _fingerprint({"action": PLAYER_LINK_REVIEW_ACTION, "approval_fingerprint": fingerprint})
    review_key = _review_required_idempotency_key(values["idempotency_key"])
    _begin_sqlite_outer_transaction_if_needed(session)

    try:
        with session.begin_nested():
            audit = _load_audit(session, values["idempotency_key"])
            if audit is not None:
                return _idempotent_approval(session, audit, fingerprint)
            review_audit = _load_audit(session, review_key)
            if review_audit is not None:
                return _idempotent_mutation(session, review_audit, PLAYER_LINK_REVIEW_ACTION, review_fingerprint)

            request_hint = session.get(PlayerLinkRequest, values["request_id"])
            candidate_hint = session.get(GameAccount, values["selected_game_account_id"])
            if request_hint is None or candidate_hint is None:
                raise PlayerLinkRequestConflictError("player-link request or candidate does not exist")
            candidate_persona = _lock_candidate_persona(
                session,
                persona_id=candidate_hint.persona_id,
            )
            if (
                _current_candidate_fingerprint(session, candidate_id=candidate_hint.id)
                != values["expected_candidate_fingerprint"]
            ):
                request = _load_request_for_update(session, values["request_id"])
                if request is None or request.status not in ACTIVE_PLAYER_LINK_STATUSES:
                    raise PlayerLinkRequestConflictError("player-link request is no longer active")
                return _mark_review_required_after_snapshot_change(
                    session,
                    request=request,
                    actor_discord_user_id=values["reviewed_by_discord_user_id"],
                    idempotency_key=review_key,
                    request_fingerprint=review_fingerprint,
                )

            requester_account = _lock_or_create_discord_account_for_link(
                session,
                discord_user_id=request_hint.requester_discord_user_id,
                discord_nickname=request_hint.discord_nickname_snapshot,
            )
            locked_discord_accounts = session.scalars(
                select(DiscordAccount)
                .where(DiscordAccount.id.in_({requester_account.id, candidate_hint.discord_account_id}))
                .order_by(DiscordAccount.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            discord_by_id = {account.id: account for account in locked_discord_accounts}
            if candidate_hint.discord_account_id not in discord_by_id:
                raise PlayerLinkRequestConflictError("candidate Discord account does not exist")
            requester_account = discord_by_id.get(requester_account.id, requester_account)

            request = _load_request_for_update(session, values["request_id"])
            audit = _load_audit(session, values["idempotency_key"], lock=True)
            if audit is not None:
                return _idempotent_approval(session, audit, fingerprint)
            review_audit = _load_audit(session, review_key, lock=True)
            if review_audit is not None:
                return _idempotent_mutation(session, review_audit, PLAYER_LINK_REVIEW_ACTION, review_fingerprint)
            if request is None or request.status not in ACTIVE_PLAYER_LINK_STATUSES:
                raise PlayerLinkRequestConflictError("player-link request is no longer active")

            accounts = session.scalars(
                select(GameAccount)
                .where(
                    or_(
                        GameAccount.id == values["selected_game_account_id"],
                        GameAccount.discord_account_id == requester_account.id,
                        GameAccount.uma_pid == request.submitted_uma_pid,
                    )
                )
                .order_by(GameAccount.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            candidate = next(
                (account for account in accounts if account.id == values["selected_game_account_id"]),
                None,
            )
            if candidate is None:
                raise PlayerLinkRequestConflictError("selected player-link candidate no longer exists")
            _assert_approvable_candidate(candidate)
            if candidate.persona_id != candidate_persona.id:
                raise PlayerLinkRequestConflictError("selected account Persona ownership changed")
            if requester_account.persona_id not in {None, candidate_persona.id}:
                raise PlayerLinkRequestConflictError("requester Discord account belongs to a different Persona")

            requester_owner = next(
                (account for account in accounts if account.discord_account_id == requester_account.id),
                None,
            )
            if requester_owner is not None and requester_owner.id != candidate.id:
                raise AccountAlreadyRegisteredError("Discord user already owns a game account")
            pid_owner = next(
                (
                    account
                    for account in accounts
                    if account.uma_pid == request.submitted_uma_pid and account.id != candidate.id
                ),
                None,
            )
            if pid_owner is not None:
                raise PlayerLinkRequestConflictError("UMA PID is already registered")

            selected_request = session.scalar(
                select(PlayerLinkRequest)
                .where(
                    PlayerLinkRequest.selected_game_account_id == candidate.id,
                    PlayerLinkRequest.id != request.id,
                )
                .with_for_update()
            )
            if selected_request is not None:
                raise PlayerLinkRequestConflictError("selected legacy account is already linked")

            task, record, run = _load_candidate_provenance(session, candidate_id=candidate.id, lock=True)
            point_account = session.scalar(
                select(CirclePointAccount)
                .where(CirclePointAccount.persona_id == candidate.persona_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if point_account is None:
                raise PlayerLinkRequestConflictError("candidate room point account does not exist")
            ledger_count, latest_transaction_id = _locked_ledger_snapshot(session, game_account_id=candidate.id)
            current_candidate_fingerprint = _candidate_fingerprint(
                candidate=candidate,
                point_account=point_account,
                ledger_count=ledger_count,
                latest_transaction_id=latest_transaction_id,
                task=task,
                record=record,
                run=run,
            )
            if current_candidate_fingerprint != values["expected_candidate_fingerprint"]:
                return _mark_review_required_after_snapshot_change(
                    session,
                    request=request,
                    actor_discord_user_id=values["reviewed_by_discord_user_id"],
                    idempotency_key=review_key,
                    request_fingerprint=review_fingerprint,
                )

            before = _approval_snapshot(
                request=request,
                candidate=candidate,
                point_account=point_account,
                ledger_count=ledger_count,
                latest_transaction_id=latest_transaction_id,
                previous_discord_account_id=candidate.discord_account_id,
                current_discord_account_id=requester_account.id,
                candidate_persona=candidate_persona,
                requester_persona_id=requester_account.persona_id,
            )
            candidate.discord_account_id = requester_account.id
            candidate.uma_pid = request.submitted_uma_pid
            candidate.ingame_name = request.submitted_ingame_name
            candidate.identity_status = IdentityStatus.CONFIRMED.value
            requester_account.discord_nickname = request.discord_nickname_snapshot
            requester_account.persona_id = candidate_persona.id
            candidate_persona.status = "active"
            task.status = "resolved"
            task.resolved_by_discord_user_id = values["reviewed_by_discord_user_id"]
            task.resolved_at = datetime.now(UTC)
            task.resolution_note = values["review_note"]
            task.conflict_detail_json = None
            request.status = "approved"
            request.active_request_marker = None
            request.selected_game_account_id = candidate.id
            request.reviewed_by_discord_user_id = values["reviewed_by_discord_user_id"]
            request.review_note = values["review_note"]
            request.resolved_at = datetime.now(UTC)
            session.flush()
            after = _approval_snapshot(
                request=request,
                candidate=candidate,
                point_account=point_account,
                ledger_count=ledger_count,
                latest_transaction_id=latest_transaction_id,
                previous_discord_account_id=before["previous_discord_account_id"],
                current_discord_account_id=requester_account.id,
                candidate_persona=candidate_persona,
                requester_persona_id=requester_account.persona_id,
            )
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_APPROVE_ACTION,
                actor_discord_user_id=values["reviewed_by_discord_user_id"],
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=after,
                reason=values["review_note"],
            )
            session.add(audit)
            session.flush()
            return _approval_dto(request=request, audit_id=audit.id, snapshot=after)
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, values["idempotency_key"], lock=True)
            if audit is not None:
                return _idempotent_approval(session, audit, fingerprint)
        raise PlayerLinkRequestConflictError("player-link approval conflicted") from exc


def mark_player_link_request_for_review(
    session: Session,
    *,
    command: ReviewPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    values = _normalize_review_command(command)
    fingerprint = _fingerprint({"action": PLAYER_LINK_REVIEW_ACTION, **values})
    return _set_player_link_request_review_required(session, values=values, fingerprint=fingerprint)


def reject_player_link_request(
    session: Session,
    *,
    command: RejectPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    values = _normalize_reject_command(command)
    fingerprint = _fingerprint({"action": PLAYER_LINK_REJECT_ACTION, **values})
    _begin_sqlite_outer_transaction_if_needed(session)
    try:
        with session.begin_nested():
            audit = _load_audit(session, values["idempotency_key"])
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REJECT_ACTION, fingerprint)
            request = _load_request_for_update(session, values["request_id"])
            if request is None or request.status not in ACTIVE_PLAYER_LINK_STATUSES:
                raise PlayerLinkRequestConflictError("player-link request is no longer active")
            before = _request_json(request)
            request.status = "rejected"
            request.active_request_marker = None
            request.reviewed_by_discord_user_id = values["reviewed_by_discord_user_id"]
            request.review_note = values["review_note"]
            request.resolved_at = datetime.now(UTC)
            session.flush()
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_REJECT_ACTION,
                actor_discord_user_id=values["reviewed_by_discord_user_id"],
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=_request_json(request),
                reason=values["review_note"],
            )
            session.add(audit)
            session.flush()
            return PlayerLinkMutationDTO(
                action=PLAYER_LINK_REJECT_ACTION,
                audit_id=audit.id,
                request=_request_dto(request),
            )
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, values["idempotency_key"], lock=True)
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REJECT_ACTION, fingerprint)
        raise PlayerLinkRequestConflictError("player-link request rejection conflicted") from exc


def search_player_link_candidates(
    session: Session,
    *,
    request_id: int,
    limit: int = 10,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    normalized_request_id = _positive_id(request_id, field="player-link request ID")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise PlayerLinkRequestError("candidate limit must be between 1 and 10")
    request = session.get(PlayerLinkRequest, normalized_request_id)
    if request is None:
        raise PlayerLinkRequestConflictError("player-link request does not exist")
    if request.status not in ACTIVE_PLAYER_LINK_STATUSES:
        raise PlayerLinkRequestConflictError("player-link request is no longer active")

    return _search_player_link_candidates_for_terms(
        session,
        terms=PlayerLinkCandidateSearchTerms(
            submitted_ingame_name=request.submitted_ingame_name,
            discord_nickname_snapshot=request.discord_nickname_snapshot,
            submitted_nickname_chunk=request.submitted_nickname_chunk,
        ),
        limit=limit,
    )


def search_player_link_candidates_for_terms(
    session: Session,
    *,
    submitted_ingame_name: str,
    discord_nickname_snapshot: str,
    submitted_nickname_chunk: str | None,
    limit: int = 10,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise PlayerLinkRequestError("candidate limit must be between 1 and 10")
    terms = PlayerLinkCandidateSearchTerms(
        submitted_ingame_name=_required_text(
            submitted_ingame_name,
            field="in-game name",
            maximum=100,
        ),
        discord_nickname_snapshot=_required_text(
            discord_nickname_snapshot,
            field="Discord nickname",
            maximum=100,
        ),
        submitted_nickname_chunk=_optional_text(
            submitted_nickname_chunk,
            field="legacy nickname chunk",
            maximum=100,
        ),
    )
    return _search_player_link_candidates_for_terms(session, terms=terms, limit=limit)


def get_active_player_link_request(
    session: Session,
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    request = _load_active_request(
        session,
        guild_id=_discord_id(guild_id, field="guild ID"),
        requester_discord_user_id=_discord_id(requester_discord_user_id, field="requester Discord user ID"),
        lock=False,
    )
    return _request_dto(request) if request is not None else None


def _search_player_link_candidates_for_terms(
    session: Session,
    *,
    terms: PlayerLinkCandidateSearchTerms,
    limit: int,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    approved_request = PlayerLinkRequest.__table__.alias("approved_player_link_request")
    transaction_count = func.count(CirclePointTransaction.id)
    last_transaction_at = func.max(CirclePointTransaction.created_at)
    rows = session.execute(
        select(
            GameAccount.id,
            GameAccount.discord_account_id,
            GameAccount.nickname,
            GameAccount.ingame_name,
            GameAccount.identity_status,
            GameAccount.updated_at,
            DiscordAccount.discord_nickname,
            CirclePointAccount.id.label("point_account_id"),
            CirclePointAccount.balance,
            SheetImportRun.source_identifier,
            SheetImportRun.source_checksum,
            SheetImportRecord.id.label("source_import_record_id"),
            transaction_count.label("ledger_count"),
            func.max(CirclePointTransaction.id).label("latest_transaction_id"),
            last_transaction_at.label("last_transaction_at"),
        )
        .join(DiscordAccount, DiscordAccount.id == GameAccount.discord_account_id)
        .join(IdentityBackfillTask, IdentityBackfillTask.game_account_id == GameAccount.id)
        .join(SheetImportRecord, SheetImportRecord.id == IdentityBackfillTask.source_import_record_id)
        .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
        .join(CirclePointAccount, CirclePointAccount.persona_id == GameAccount.persona_id)
        .outerjoin(CirclePointTransaction, CirclePointTransaction.game_account_id == GameAccount.id)
        .where(
            GameAccount.identity_status.in_(_CANDIDATE_IDENTITY_STATUSES),
            IdentityBackfillTask.status.in_(_CANDIDATE_TASK_STATUSES),
            SheetImportRun.import_kind == LEGACY_IDENTITY_POINT_IMPORT_KIND,
            SheetImportRun.status == "completed",
            SheetImportRecord.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE,
            SheetImportRecord.status == "applied",
            SheetImportRecord.target_entity_type == "game_account",
            SheetImportRecord.target_entity_id == GameAccount.id,
            ~exists(
                select(1)
                .select_from(approved_request)
                .where(
                    approved_request.c.selected_game_account_id == GameAccount.id,
                    approved_request.c.status == "approved",
                )
            ),
        )
        .group_by(
            GameAccount.id,
            GameAccount.discord_account_id,
            GameAccount.nickname,
            GameAccount.ingame_name,
            GameAccount.identity_status,
            GameAccount.updated_at,
            DiscordAccount.discord_nickname,
            CirclePointAccount.id,
            CirclePointAccount.balance,
            SheetImportRun.source_identifier,
            SheetImportRun.source_checksum,
            SheetImportRecord.id,
        )
        .order_by(GameAccount.id)
    ).all()

    candidates = [candidate for row in rows if (candidate := _candidate_dto(terms, row)) is not None]
    candidates.sort(key=lambda candidate: (-candidate.match_score, candidate.game_account_id))
    return tuple(candidates[:limit])


def assert_no_active_player_link_request_for_registration(
    session: Session,
    *,
    requester_discord_user_id: str,
) -> None:
    requester = _discord_id(requester_discord_user_id, field="Discord user ID")
    active_request = session.scalar(
        select(PlayerLinkRequest.id)
        .where(
            PlayerLinkRequest.requester_discord_user_id == requester,
            PlayerLinkRequest.active_request_marker == 1,
        )
        .order_by(PlayerLinkRequest.id)
        .limit(1)
        .with_for_update()
    )
    if active_request is not None:
        raise PlayerLinkRequestConflictError("an active player-link request must be resolved before registration")


def _normalize_approval_command(command: ApprovePlayerLinkRequestCommand) -> dict[str, str | int | None]:
    expected_fingerprint = _required_text(
        command.expected_candidate_fingerprint,
        field="candidate fingerprint",
        maximum=64,
    )
    if len(expected_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in expected_fingerprint
    ):
        raise PlayerLinkRequestError("candidate fingerprint must be a SHA-256 hex value")
    return {
        "request_id": _positive_id(command.request_id, field="player-link request ID"),
        "selected_game_account_id": _positive_id(command.selected_game_account_id, field="game account ID"),
        "expected_candidate_fingerprint": expected_fingerprint,
        "reviewed_by_discord_user_id": _discord_id(
            command.reviewed_by_discord_user_id,
            field="reviewer Discord user ID",
        ),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", maximum=128),
        "review_note": _optional_text(command.review_note, field="review note", maximum=255),
    }


def _normalize_review_command(command: ReviewPlayerLinkRequestCommand) -> dict[str, str | int]:
    return {
        "request_id": _positive_id(command.request_id, field="player-link request ID"),
        "reviewed_by_discord_user_id": _discord_id(
            command.reviewed_by_discord_user_id,
            field="reviewer Discord user ID",
        ),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", maximum=128),
        "review_note": _required_text(command.review_note, field="review note", maximum=255),
    }


def _normalize_reject_command(command: RejectPlayerLinkRequestCommand) -> dict[str, str | int]:
    return {
        "request_id": _positive_id(command.request_id, field="player-link request ID"),
        "reviewed_by_discord_user_id": _discord_id(
            command.reviewed_by_discord_user_id,
            field="reviewer Discord user ID",
        ),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", maximum=128),
        "review_note": _required_text(command.review_note, field="rejection note", maximum=255),
    }


def _set_player_link_request_review_required(
    session: Session,
    *,
    values: dict[str, str | int],
    fingerprint: str,
) -> PlayerLinkMutationDTO:
    _begin_sqlite_outer_transaction_if_needed(session)
    try:
        with session.begin_nested():
            audit = _load_audit(session, str(values["idempotency_key"]))
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REVIEW_ACTION, fingerprint)
            request = _load_request_for_update(session, int(values["request_id"]))
            if request is None or request.status not in ACTIVE_PLAYER_LINK_STATUSES:
                raise PlayerLinkRequestConflictError("player-link request is no longer active")
            before = _request_json(request)
            request.status = "review_required"
            request.review_note = str(values["review_note"])
            session.flush()
            audit = _new_audit(
                request=request,
                action=PLAYER_LINK_REVIEW_ACTION,
                actor_discord_user_id=str(values["reviewed_by_discord_user_id"]),
                idempotency_key=str(values["idempotency_key"]),
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=_request_json(request),
                reason=str(values["review_note"]),
            )
            session.add(audit)
            session.flush()
            return PlayerLinkMutationDTO(
                action=PLAYER_LINK_REVIEW_ACTION,
                audit_id=audit.id,
                request=_request_dto(request),
            )
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, str(values["idempotency_key"]), lock=True)
            if audit is not None:
                return _idempotent_mutation(session, audit, PLAYER_LINK_REVIEW_ACTION, fingerprint)
        raise PlayerLinkRequestConflictError("player-link request review conflicted") from exc


def _mark_review_required_after_snapshot_change(
    session: Session,
    *,
    request: PlayerLinkRequest,
    actor_discord_user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> PlayerLinkMutationDTO:
    before = _request_json(request)
    request.status = "review_required"
    request.review_note = "candidate point or ledger state changed; review candidate again"
    session.flush()
    audit = _new_audit(
        request=request,
        action=PLAYER_LINK_REVIEW_ACTION,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        before_json=before,
        after_json=_request_json(request),
        reason=request.review_note,
    )
    session.add(audit)
    session.flush()
    return PlayerLinkMutationDTO(
        action=PLAYER_LINK_REVIEW_ACTION,
        audit_id=audit.id,
        request=_request_dto(request),
    )


def _load_request_for_update(session: Session, request_id: int) -> PlayerLinkRequest | None:
    return session.scalar(
        select(PlayerLinkRequest)
        .where(PlayerLinkRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_or_create_discord_account_for_link(
    session: Session,
    *,
    discord_user_id: str,
    discord_nickname: str,
) -> DiscordAccount:
    if session.bind is not None and session.bind.dialect.name in {"mysql", "mariadb"}:
        session.execute(
            mysql_insert(DiscordAccount)
            .values(discord_user_id=discord_user_id, discord_nickname=discord_nickname)
            .on_duplicate_key_update(id=DiscordAccount.id)
        )
    account = session.scalar(
        select(DiscordAccount)
        .where(DiscordAccount.discord_user_id == discord_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is not None:
        return account
    account = DiscordAccount(discord_user_id=discord_user_id, discord_nickname=discord_nickname)
    session.add(account)
    session.flush()
    return account


def _assert_approvable_candidate(candidate: GameAccount) -> None:
    if candidate.identity_status not in _CANDIDATE_IDENTITY_STATUSES:
        raise PlayerLinkRequestConflictError("selected account is not an unresolved legacy candidate")


def _lock_candidate_persona(
    session: Session,
    *,
    persona_id: str | None,
) -> Persona:
    if persona_id is None:
        raise PlayerLinkRequestConflictError("selected account has no Persona owner")
    persona = session.scalar(
        select(Persona).where(Persona.id == persona_id).with_for_update().execution_options(populate_existing=True)
    )
    if persona is None:
        raise PlayerLinkRequestConflictError("selected account Persona owner does not exist")
    if persona.status not in {"active", "inactive"}:
        raise PlayerLinkRequestConflictError("selected account Persona is not eligible for activation")
    return persona


def _load_candidate_provenance(
    session: Session,
    *,
    candidate_id: int,
    lock: bool,
) -> tuple[IdentityBackfillTask, SheetImportRecord, SheetImportRun]:
    task_query = select(IdentityBackfillTask).where(IdentityBackfillTask.game_account_id == candidate_id)
    if lock:
        task_query = task_query.with_for_update().execution_options(populate_existing=True)
    task = session.scalar(task_query)
    if task is None or task.status not in _CANDIDATE_TASK_STATUSES or task.source_import_record_id is None:
        raise PlayerLinkRequestConflictError("selected account has no unresolved legacy provenance")
    record_query = select(SheetImportRecord).where(SheetImportRecord.id == task.source_import_record_id)
    if lock:
        record_query = record_query.with_for_update().execution_options(populate_existing=True)
    record = session.scalar(record_query)
    if (
        record is None
        or record.record_type != LEGACY_IDENTITY_POINT_RECORD_TYPE
        or record.status != "applied"
        or record.target_entity_type != "game_account"
        or record.target_entity_id != candidate_id
    ):
        raise PlayerLinkRequestConflictError("selected account legacy provenance is invalid")
    run_query = select(SheetImportRun).where(SheetImportRun.id == record.import_run_id)
    if lock:
        run_query = run_query.with_for_update().execution_options(populate_existing=True)
    run = session.scalar(run_query)
    if (
        run is None
        or run.import_kind != LEGACY_IDENTITY_POINT_IMPORT_KIND
        or run.source_type != "xlsx"
        or run.status != "completed"
        or run.finished_at is None
    ):
        raise PlayerLinkRequestConflictError("selected account import run is not approved for linking")
    return task, record, run


def _current_candidate_fingerprint(session: Session, *, candidate_id: int) -> str:
    candidate = session.get(GameAccount, candidate_id)
    if candidate is None:
        raise PlayerLinkRequestConflictError("selected player-link candidate no longer exists")
    _assert_approvable_candidate(candidate)
    task, record, run = _load_candidate_provenance(session, candidate_id=candidate_id, lock=False)
    point_account = (
        session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == candidate.persona_id))
        if candidate.persona_id is not None
        else None
    )
    if point_account is None:
        raise PlayerLinkRequestConflictError("candidate room point account does not exist")
    ledger_count, latest_transaction_id = _locked_ledger_snapshot(session, game_account_id=candidate_id)
    return _candidate_fingerprint(
        candidate=candidate,
        point_account=point_account,
        ledger_count=ledger_count,
        latest_transaction_id=latest_transaction_id,
        task=task,
        record=record,
        run=run,
    )


def _locked_ledger_snapshot(session: Session, *, game_account_id: int) -> tuple[int, int | None]:
    row = session.execute(
        select(
            func.count(CirclePointTransaction.id),
            func.max(CirclePointTransaction.id),
        ).where(CirclePointTransaction.game_account_id == game_account_id)
    ).one()
    return int(row[0]), (int(row[1]) if row[1] is not None else None)


def _candidate_fingerprint(
    *,
    candidate: GameAccount,
    point_account: CirclePointAccount,
    ledger_count: int,
    latest_transaction_id: int | None,
    task: IdentityBackfillTask,
    record: SheetImportRecord,
    run: SheetImportRun,
) -> str:
    return _fingerprint(
        {
            "game_account_id": candidate.id,
            "discord_account_id": candidate.discord_account_id,
            "identity_status": candidate.identity_status,
            "point_account_id": point_account.id,
            "point_balance": point_account.balance,
            "ledger_count": ledger_count,
            "latest_transaction_id": latest_transaction_id,
            "source_import_record_id": task.source_import_record_id,
            "source_checksum": run.source_checksum,
        }
    )


def _approval_snapshot(
    *,
    request: PlayerLinkRequest,
    candidate: GameAccount,
    point_account: CirclePointAccount,
    ledger_count: int,
    latest_transaction_id: int | None,
    previous_discord_account_id: int,
    current_discord_account_id: int,
    candidate_persona: Persona,
    requester_persona_id: str | None,
) -> dict[str, object]:
    return {
        "request": _request_json(request),
        "game_account_id": candidate.id,
        # Persisted audit keys retain the pre-cutover storage contract.
        "room_point_account_id": point_account.id,
        "room_point_balance": point_account.balance,
        "ledger_count": ledger_count,
        "latest_transaction_id": latest_transaction_id,
        "previous_discord_account_id": previous_discord_account_id,
        "current_discord_account_id": current_discord_account_id,
        "persona_id": candidate_persona.id,
        "persona_status": candidate_persona.status,
        "requester_persona_id": requester_persona_id,
    }


def _approval_dto(
    *,
    request: PlayerLinkRequest,
    audit_id: int,
    snapshot: dict[str, object],
) -> PlayerLinkApprovalDTO:
    return PlayerLinkApprovalDTO(
        audit_id=audit_id,
        request=_request_dto(request),
        game_account_id=int(snapshot["game_account_id"]),
        circle_point_account_id=int(snapshot["room_point_account_id"]),
        circle_point_balance=int(snapshot["room_point_balance"]),
        ledger_count=int(snapshot["ledger_count"]),
        latest_transaction_id=(
            int(snapshot["latest_transaction_id"]) if snapshot["latest_transaction_id"] is not None else None
        ),
        previous_discord_account_id=int(snapshot["previous_discord_account_id"]),
        current_discord_account_id=int(snapshot["current_discord_account_id"]),
    )


def _idempotent_approval(
    session: Session,
    audit: PlayerLinkOperationAudit,
    fingerprint: str,
) -> PlayerLinkApprovalDTO:
    if audit.action != PLAYER_LINK_APPROVE_ACTION or audit.request_fingerprint != fingerprint:
        raise PlayerLinkRequestConflictError("idempotency key payload does not match the original player-link action")
    request = session.get(PlayerLinkRequest, audit.player_link_request_id)
    if request is None or not isinstance(audit.after_json, dict):
        raise PlayerLinkRequestConflictError("player-link approval audit is invalid")
    return _approval_dto(request=request, audit_id=audit.id, snapshot=audit.after_json)


def _review_required_idempotency_key(approval_key: str) -> str:
    return f"player-link-review:{sha256(approval_key.encode('utf-8')).hexdigest()}"


def _normalize_submit_command(command: SubmitPlayerLinkRequestCommand) -> dict[str, str | None]:
    return {
        "guild_id": _discord_id(command.guild_id, field="guild ID"),
        "requester_discord_user_id": _discord_id(
            command.requester_discord_user_id,
            field="requester Discord user ID",
        ),
        "discord_nickname_snapshot": _required_text(
            command.discord_nickname_snapshot,
            field="Discord nickname",
            maximum=100,
        ),
        "submitted_ingame_name": _required_text(
            command.submitted_ingame_name,
            field="in-game name",
            maximum=100,
        ),
        "submitted_uma_pid": validate_registration_pid(command.submitted_uma_pid),
        "submitted_nickname_chunk": _optional_text(
            command.submitted_nickname_chunk,
            field="legacy nickname chunk",
            maximum=100,
        ),
        "submitted_participation_hint": _optional_text(
            command.submitted_participation_hint,
            field="participation hint",
            maximum=255,
        ),
        "requester_note": _optional_text(command.requester_note, field="requester note", maximum=1000),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", maximum=128),
    }


def _normalize_revise_command(command: RevisePlayerLinkRequestCommand) -> dict[str, str | None | int]:
    values = _normalize_submit_command(
        SubmitPlayerLinkRequestCommand(
            guild_id=command.guild_id,
            requester_discord_user_id=command.requester_discord_user_id,
            discord_nickname_snapshot=command.discord_nickname_snapshot,
            submitted_ingame_name=command.submitted_ingame_name,
            submitted_uma_pid=command.submitted_uma_pid,
            idempotency_key=command.idempotency_key,
            submitted_nickname_chunk=command.submitted_nickname_chunk,
            submitted_participation_hint=command.submitted_participation_hint,
            requester_note=command.requester_note,
        )
    )
    return {"request_id": _positive_id(command.request_id, field="player-link request ID"), **values}


def _optional_submitter_id(
    submitted_by_discord_user_id: str | None,
    values: dict[str, str | None],
) -> str:
    if submitted_by_discord_user_id is None:
        return str(values["requester_discord_user_id"])
    return _discord_id(submitted_by_discord_user_id, field="submitter Discord user ID")


def _recover_submit_integrity_conflict(
    session: Session,
    *,
    error: IntegrityError,
    values: dict[str, str | None],
    fingerprint: str,
) -> PlayerLinkMutationDTO:
    with session.begin_nested():
        audit = _load_audit(session, str(values["idempotency_key"]), lock=True)
        if audit is not None:
            return _idempotent_mutation(session, audit, PLAYER_LINK_SUBMIT_ACTION, fingerprint)
        active_request = _load_active_request(
            session,
            guild_id=str(values["guild_id"]),
            requester_discord_user_id=str(values["requester_discord_user_id"]),
            lock=True,
        )
        if active_request is not None:
            raise PlayerLinkRequestConflictError("an active player-link request already exists") from error
    raise PlayerLinkRequestConflictError("player-link request submission conflicted") from error


def _raise_if_requester_already_registered(session: Session, requester_discord_user_id: str) -> None:
    existing_account = session.scalar(
        select(GameAccount)
        .join(DiscordAccount, DiscordAccount.id == GameAccount.discord_account_id)
        .where(DiscordAccount.discord_user_id == requester_discord_user_id)
        .with_for_update()
    )
    if existing_account is not None and existing_account.identity_status == IdentityStatus.CONFIRMED.value:
        raise AccountAlreadyRegisteredError("Discord user already owns a game account")


def _load_active_request(
    session: Session,
    *,
    guild_id: str,
    requester_discord_user_id: str,
    lock: bool,
) -> PlayerLinkRequest | None:
    query = (
        select(PlayerLinkRequest)
        .where(
            PlayerLinkRequest.guild_id == guild_id,
            PlayerLinkRequest.requester_discord_user_id == requester_discord_user_id,
            PlayerLinkRequest.active_request_marker == 1,
        )
        .order_by(PlayerLinkRequest.id)
        .limit(1)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _load_audit(
    session: Session,
    idempotency_key: str,
    *,
    lock: bool = False,
) -> PlayerLinkOperationAudit | None:
    query = select(PlayerLinkOperationAudit).where(PlayerLinkOperationAudit.idempotency_key == idempotency_key)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _idempotent_mutation(
    session: Session,
    audit: PlayerLinkOperationAudit,
    action: str,
    fingerprint: str,
) -> PlayerLinkMutationDTO:
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise PlayerLinkRequestConflictError("idempotency key payload does not match the original player-link action")
    request = session.get(PlayerLinkRequest, audit.player_link_request_id)
    if request is None:
        raise PlayerLinkRequestConflictError("player-link audit refers to a missing request")
    return PlayerLinkMutationDTO(action=action, audit_id=audit.id, request=_request_dto(request))


def _new_audit(
    *,
    request: PlayerLinkRequest,
    action: str,
    actor_discord_user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    before_json: dict[str, object] | None,
    after_json: dict[str, object],
    reason: str | None,
) -> PlayerLinkOperationAudit:
    return PlayerLinkOperationAudit(
        player_link_request_id=request.id,
        action=action,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        before_json=before_json,
        after_json=after_json,
        reason=reason,
    )


def _request_dto(request: PlayerLinkRequest) -> PlayerLinkRequestDTO:
    return PlayerLinkRequestDTO(
        id=request.id,
        guild_id=request.guild_id,
        requester_discord_user_id=request.requester_discord_user_id,
        discord_nickname_snapshot=request.discord_nickname_snapshot,
        submitted_ingame_name=request.submitted_ingame_name,
        submitted_uma_pid=request.submitted_uma_pid,
        submitted_nickname_chunk=request.submitted_nickname_chunk,
        submitted_participation_hint=request.submitted_participation_hint,
        requester_note=request.requester_note,
        status=request.status,
        selected_game_account_id=request.selected_game_account_id,
        reviewed_by_discord_user_id=request.reviewed_by_discord_user_id,
        review_note=request.review_note,
        created_at=database_datetime_as_utc(request.created_at),
        updated_at=database_datetime_as_utc(request.updated_at),
        resolved_at=(database_datetime_as_utc(request.resolved_at) if request.resolved_at is not None else None),
    )


def _request_json(request: PlayerLinkRequest) -> dict[str, object]:
    return {
        "id": request.id,
        "guild_id": request.guild_id,
        "requester_discord_user_id": request.requester_discord_user_id,
        "status": request.status,
        "submitted_ingame_name": request.submitted_ingame_name,
        "submitted_uma_pid": request.submitted_uma_pid,
        "selected_game_account_id": request.selected_game_account_id,
        "reviewed_by_discord_user_id": request.reviewed_by_discord_user_id,
        "review_note": request.review_note,
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at is not None else None,
    }


def _candidate_dto(terms: PlayerLinkCandidateSearchTerms, row: object) -> PlayerLinkCandidateDTO | None:
    game_account_id = int(row.id)
    legacy_nickname = row.nickname
    ingame_name = row.ingame_name
    discord_nickname = str(row.discord_nickname)
    reasons: list[str] = []
    score = 0

    submitted_ingame = terms.submitted_ingame_name
    _score_name_match(
        reasons,
        submitted_ingame,
        ingame_name,
        label="현재 인게임 닉네임",
        exact_score=1_000,
        relaxed_score=700,
    )
    _score_name_match(
        reasons,
        submitted_ingame,
        legacy_nickname,
        label="레거시 닉네임",
        exact_score=900,
        relaxed_score=650,
    )
    _score_name_match(
        reasons,
        terms.discord_nickname_snapshot,
        discord_nickname,
        label="Discord 닉네임",
        exact_score=850,
        relaxed_score=600,
    )
    score += _score_from_reasons(reasons)

    if terms.submitted_nickname_chunk is not None:
        chunk = normalize_player_name_relaxed(terms.submitted_nickname_chunk)
        if chunk:
            for candidate_name, label in (
                (legacy_nickname, "레거시 닉네임"),
                (ingame_name, "인게임 닉네임"),
                (discord_nickname, "Discord 닉네임"),
            ):
                candidate_key = normalize_player_name_relaxed(candidate_name or "")
                if chunk in candidate_key:
                    reasons.append(f"기억한 문자열이 {label}에 포함")
                    score += 300
                    break

    if not reasons:
        similarity = max(
            (
                player_name_similarity(submitted_ingame, candidate_name)
                for candidate_name in (legacy_nickname, ingame_name, discord_nickname)
                if candidate_name
            ),
            default=0.0,
        )
        if similarity >= 0.65:
            reasons.append("현재 인게임 닉네임 철자 유사")
            score += int(similarity * 100)
    if not reasons:
        return None

    updated_at = database_datetime_as_utc(row.updated_at)
    transaction_at = row.last_transaction_at
    last_activity_at = updated_at
    if transaction_at is not None:
        normalized_transaction_at = database_datetime_as_utc(transaction_at)
        if normalized_transaction_at > last_activity_at:
            last_activity_at = normalized_transaction_at
    return PlayerLinkCandidateDTO(
        game_account_id=game_account_id,
        candidate_fingerprint=_candidate_fingerprint_from_row(row),
        legacy_nickname=legacy_nickname,
        ingame_name=ingame_name,
        discord_nickname=discord_nickname,
        current_balance=int(row.balance),
        ledger_count=int(row.ledger_count),
        last_activity_at=last_activity_at,
        import_source=str(row.source_identifier),
        identity_status=str(row.identity_status),
        match_score=score,
        match_reasons=tuple(reasons),
    )


def _candidate_fingerprint_from_row(row: object) -> str:
    return _fingerprint(
        {
            "game_account_id": int(row.id),
            "discord_account_id": int(row.discord_account_id),
            "identity_status": str(row.identity_status),
            "point_account_id": int(row.point_account_id),
            "point_balance": int(row.balance),
            "ledger_count": int(row.ledger_count),
            "latest_transaction_id": (
                int(row.latest_transaction_id) if row.latest_transaction_id is not None else None
            ),
            "source_import_record_id": int(row.source_import_record_id),
            "source_checksum": str(row.source_checksum),
        }
    )


def _score_name_match(
    reasons: list[str],
    source: str,
    candidate: str | None,
    *,
    label: str,
    exact_score: int,
    relaxed_score: int,
) -> None:
    if candidate is None:
        return
    if normalize_player_name_strict(source) == normalize_player_name_strict(candidate):
        reasons.append(f"{label} 엄격 일치")
        reasons.append(f"_score:{exact_score}")
    elif normalize_player_name_relaxed(source) == normalize_player_name_relaxed(candidate):
        reasons.append(f"{label} 완화 일치")
        reasons.append(f"_score:{relaxed_score}")


def _score_from_reasons(reasons: list[str]) -> int:
    score = 0
    retained: list[str] = []
    for reason in reasons:
        if reason.startswith("_score:"):
            score += int(reason.removeprefix("_score:"))
        else:
            retained.append(reason)
    reasons[:] = retained
    return score


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def _begin_sqlite_outer_transaction_if_needed(session: Session) -> None:
    if session.in_transaction() or session.bind is None or session.bind.dialect.name != "sqlite":
        return
    session.connection().exec_driver_sql("BEGIN")


def _discord_id(value: str, *, field: str) -> str:
    normalized = _required_text(value, field=field, maximum=32)
    if not normalized.isascii() or not normalized.isdigit():
        raise PlayerLinkRequestError(f"{field} must contain 1 to 32 ASCII digits")
    return normalized


def _positive_id(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PlayerLinkRequestError(f"{field} must be a positive integer")
    return value


def _required_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlayerLinkRequestError(f"{field} must be text")
    normalized = value.strip()
    if not normalized:
        raise PlayerLinkRequestError(f"{field} is required")
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise PlayerLinkRequestError(f"{field} contains unsupported text")
    return normalized


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field, maximum=maximum)
