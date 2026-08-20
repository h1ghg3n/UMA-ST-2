from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    AccountRegistrationOperationAudit,
    AccountRegistrationRequest,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
    account_registration_initial_grant_idempotency_key,
)
from umacircle_bot.domain.errors import (
    AccountRegistrationRequestConflictError,
    AccountRegistrationRequestError,
    DuplicateUmaPidError,
)
from umacircle_bot.domain.identity import IdentityStatus, validate_registration_pid
from umacircle_bot.domain.personas import normalize_persona_id, persona_short_id
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.accounts import (
    INITIAL_CIRCLE_POINTS,
    _begin_sqlite_outer_transaction_if_needed,
    _pid_registration_lock,
)
from umacircle_bot.services.dtos import AccountRegistrationMutationDTO, AccountRegistrationRequestDTO

_ACTIVE_STATUS = "pending"


@dataclass(frozen=True, slots=True)
class SubmitAccountRegistrationRequestCommand:
    guild_id: str
    requester_discord_user_id: str
    discord_nickname_snapshot: str
    submitted_uma_pid: str
    idempotency_key: str
    submitted_nickname: str | None = None
    submitted_ingame_name: str | None = None


@dataclass(frozen=True, slots=True)
class CancelAccountRegistrationRequestCommand:
    request_id: int
    guild_id: str
    requester_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApproveAccountRegistrationRequestCommand:
    request_id: int
    reviewed_by_discord_user_id: str
    idempotency_key: str
    review_note: str | None = None
    target_persona_id: str | None = None


@dataclass(frozen=True, slots=True)
class RejectAccountRegistrationRequestCommand:
    request_id: int
    reviewed_by_discord_user_id: str
    idempotency_key: str
    review_note: str


def submit_account_registration_request(
    session: Session,
    *,
    command: SubmitAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    values = _normalize_submit(command)
    fingerprint = _fingerprint({"action": "submit", **values})
    _begin_sqlite_outer_transaction_if_needed(session)
    try:
        with session.begin_nested():
            previous = _load_audit(session, values["idempotency_key"])
            if previous is not None:
                return _idempotent(session, previous, "submit", fingerprint)
            active = _active_request(
                session,
                guild_id=values["guild_id"],
                requester_discord_user_id=values["requester_discord_user_id"],
                lock=False,
            )
            if active is not None:
                raise AccountRegistrationRequestConflictError("an active registration request already exists")
            request = AccountRegistrationRequest(
                **values,
                status=_ACTIVE_STATUS,
                active_request_marker=1,
                request_fingerprint=fingerprint,
            )
            session.add(request)
            session.flush()
            audit = _new_audit(
                request=request,
                action="submit",
                actor_discord_user_id=request.requester_discord_user_id,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                before_json=None,
                after_json=build_account_registration_request_audit_payload(request),
                reason=None,
            )
            session.add(audit)
            session.flush()
            return AccountRegistrationMutationDTO("submit", audit.id, _request_dto(session, request))
    except IntegrityError as exc:
        return _recover_submit_integrity_conflict(
            session,
            error=exc,
            values=values,
            fingerprint=fingerprint,
        )


def get_owned_account_registration_request(
    session: Session,
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> AccountRegistrationRequestDTO | None:
    request = session.scalar(
        select(AccountRegistrationRequest)
        .where(
            AccountRegistrationRequest.guild_id == _discord_id(guild_id, field="guild ID"),
            AccountRegistrationRequest.requester_discord_user_id
            == _discord_id(requester_discord_user_id, field="requester Discord user ID"),
        )
        .order_by(AccountRegistrationRequest.id.desc())
        .limit(1)
    )
    return _request_dto(session, request) if request is not None else None


def cancel_account_registration_request(
    session: Session,
    *,
    command: CancelAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    values = _normalize_cancel(command)
    fingerprint = _fingerprint({"action": "cancel", **values})
    return _resolve_request(
        session,
        action="cancel",
        request_id=values["request_id"],
        actor_discord_user_id=values["requester_discord_user_id"],
        idempotency_key=values["idempotency_key"],
        fingerprint=fingerprint,
        expected_guild_id=values["guild_id"],
        expected_requester_discord_user_id=values["requester_discord_user_id"],
        reason=values["reason"],
    )


def reject_account_registration_request(
    session: Session,
    *,
    command: RejectAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    values = _normalize_reject(command)
    fingerprint = _fingerprint({"action": "reject", **values})
    return _resolve_request(
        session,
        action="reject",
        request_id=values["request_id"],
        actor_discord_user_id=values["reviewed_by_discord_user_id"],
        idempotency_key=values["idempotency_key"],
        fingerprint=fingerprint,
        reason=values["review_note"],
    )


def approve_account_registration_request(
    session: Session,
    *,
    command: ApproveAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    values = _normalize_approve(command)
    fingerprint = _fingerprint({"action": "approve", **values})
    _begin_sqlite_outer_transaction_if_needed(session)
    try:
        with session.begin_nested():
            previous = _load_audit(session, values["idempotency_key"])
            if previous is not None:
                return _idempotent(session, previous, "approve", fingerprint)
            request = _request_for_update(session, values["request_id"])
            if request is None or request.status != _ACTIVE_STATUS:
                completed = _load_audit(session, values["idempotency_key"], lock=True)
                if completed is not None:
                    return _idempotent(session, completed, "approve", fingerprint)
                raise AccountRegistrationRequestConflictError("registration request is no longer pending")
            with _pid_registration_lock(session, request.submitted_uma_pid):
                persona, game_account = _create_approved_identity(
                    session,
                    request=request,
                    reviewed_by_discord_user_id=values["reviewed_by_discord_user_id"],
                    target_persona_id=values["target_persona_id"],
                )
            before = build_account_registration_request_audit_payload(request)
            request.status = "approved"
            request.active_request_marker = None
            request.reviewed_by_discord_user_id = values["reviewed_by_discord_user_id"]
            request.review_note = values["review_note"]
            request.accepted_persona_id = persona.id
            request.accepted_game_account_id = game_account.id
            request.resolved_at = datetime.now(UTC)
            session.flush()
            audit = _new_audit(
                request=request,
                action="approve",
                actor_discord_user_id=values["reviewed_by_discord_user_id"],
                idempotency_key=values["idempotency_key"],
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=build_account_registration_request_audit_payload(request),
                reason=values["review_note"],
            )
            session.add(audit)
            session.flush()
            return AccountRegistrationMutationDTO("approve", audit.id, _request_dto(session, request))
    except IntegrityError as exc:
        audit = _load_audit(session, values["idempotency_key"])
        if audit is not None:
            return _idempotent(session, audit, "approve", fingerprint)
        raise AccountRegistrationRequestConflictError(
            "registration approval conflicts with persisted identity state"
        ) from exc


def _resolve_request(
    session: Session,
    *,
    action: str,
    request_id: int,
    actor_discord_user_id: str,
    idempotency_key: str,
    fingerprint: str,
    reason: str | None,
    expected_guild_id: str | None = None,
    expected_requester_discord_user_id: str | None = None,
) -> AccountRegistrationMutationDTO:
    _begin_sqlite_outer_transaction_if_needed(session)
    with session.begin_nested():
        previous = _load_audit(session, idempotency_key)
        if previous is not None:
            return _idempotent(session, previous, action, fingerprint)
        request = _request_for_update(session, request_id)
        if request is None or request.status != _ACTIVE_STATUS:
            raise AccountRegistrationRequestConflictError("registration request is no longer pending")
        if expected_guild_id is not None and request.guild_id != expected_guild_id:
            raise AccountRegistrationRequestConflictError("registration request is not owned by the requester")
        if (
            expected_requester_discord_user_id is not None
            and request.requester_discord_user_id != expected_requester_discord_user_id
        ):
            raise AccountRegistrationRequestConflictError("registration request is not owned by the requester")
        before = build_account_registration_request_audit_payload(request)
        request.status = {"cancel": "cancelled", "reject": "rejected"}[action]
        request.active_request_marker = None
        request.reviewed_by_discord_user_id = actor_discord_user_id if action == "reject" else None
        request.review_note = reason
        request.resolved_at = datetime.now(UTC)
        session.flush()
        audit = _new_audit(
            request=request,
            action=action,
            actor_discord_user_id=actor_discord_user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            before_json=before,
            after_json=build_account_registration_request_audit_payload(request),
            reason=reason,
        )
        session.add(audit)
        session.flush()
        return AccountRegistrationMutationDTO(action, audit.id, _request_dto(session, request))


def _lock_or_create_discord_account(session: Session, *, discord_user_id: str, discord_nickname: str) -> DiscordAccount:
    if session.bind is not None and session.bind.dialect.name == "mysql":
        session.execute(
            mysql_insert(DiscordAccount)
            .values(discord_user_id=discord_user_id, discord_nickname=discord_nickname)
            .on_duplicate_key_update(id=DiscordAccount.id)
        )
    account = session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id).with_for_update()
    )
    if account is not None:
        account.discord_nickname = discord_nickname
        return account
    account = DiscordAccount(discord_user_id=discord_user_id, discord_nickname=discord_nickname)
    session.add(account)
    session.flush()
    return account


def _create_approved_identity(
    session: Session,
    *,
    request: AccountRegistrationRequest,
    reviewed_by_discord_user_id: str,
    target_persona_id: str | None,
) -> tuple[Persona, GameAccount]:
    owner = session.scalar(
        select(GameAccount).where(GameAccount.uma_pid == request.submitted_uma_pid).with_for_update()
    )
    if owner is not None:
        raise DuplicateUmaPidError("uma_pid is already registered to another Persona")
    discord_account = _lock_or_create_discord_account(
        session,
        discord_user_id=request.requester_discord_user_id,
        discord_nickname=request.discord_nickname_snapshot,
    )
    persona = _lock_or_create_persona(
        session,
        discord_account=discord_account,
        request=request,
        target_persona_id=target_persona_id,
    )
    existing_games = session.scalars(
        select(GameAccount).where(GameAccount.persona_id == persona.id).order_by(GameAccount.id).with_for_update()
    ).all()
    game_account = GameAccount(
        discord_account_id=discord_account.id,
        persona_id=persona.id,
        uma_pid=request.submitted_uma_pid,
        nickname=request.submitted_nickname,
        ingame_name=request.submitted_ingame_name,
        identity_status=IdentityStatus.CONFIRMED.value,
    )
    session.add(game_account)
    session.flush()
    if not existing_games:
        session.add(CirclePointAccount(persona_id=persona.id, balance=INITIAL_CIRCLE_POINTS))
        session.add(
            CirclePointTransaction(
                persona_id=persona.id,
                game_account_id=game_account.id,
                type=ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
                amount=INITIAL_CIRCLE_POINTS,
                reason="initial_room_points",
                source=ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
                created_by_discord_user_id=reviewed_by_discord_user_id,
                idempotency_key=account_registration_initial_grant_idempotency_key(request.id),
            )
        )
    return persona, game_account


def _lock_or_create_persona(
    session: Session,
    *,
    discord_account: DiscordAccount,
    request: AccountRegistrationRequest,
    target_persona_id: str | None,
) -> Persona:
    if target_persona_id is not None:
        persona = session.scalar(select(Persona).where(Persona.id == target_persona_id).with_for_update())
        if persona is None:
            raise AccountRegistrationRequestConflictError("target Persona does not exist")
        if discord_account.persona_id not in {None, persona.id}:
            raise AccountRegistrationRequestConflictError("Discord account already belongs to a different Persona")
        discord_account.persona_id = persona.id
        return persona
    if discord_account.persona_id is not None:
        persona = session.scalar(select(Persona).where(Persona.id == discord_account.persona_id).with_for_update())
        if persona is None:
            raise AccountRegistrationRequestConflictError("Discord account points to a missing Persona")
        return persona
    persona = Persona(
        display_name=request.discord_nickname_snapshot,
        display_name_source="discord",
        status="active",
    )
    session.add(persona)
    session.flush()
    discord_account.persona_id = persona.id
    return persona


def _request_for_update(session: Session, request_id: int) -> AccountRegistrationRequest | None:
    return session.scalar(
        select(AccountRegistrationRequest)
        .where(AccountRegistrationRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _active_request(
    session: Session, *, guild_id: str, requester_discord_user_id: str, lock: bool
) -> AccountRegistrationRequest | None:
    query = (
        select(AccountRegistrationRequest)
        .where(
            AccountRegistrationRequest.guild_id == guild_id,
            AccountRegistrationRequest.requester_discord_user_id == requester_discord_user_id,
            AccountRegistrationRequest.active_request_marker == 1,
        )
        .limit(1)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _recover_submit_integrity_conflict(
    session: Session,
    *,
    error: IntegrityError,
    values: dict[str, str | None],
    fingerprint: str,
) -> AccountRegistrationMutationDTO:
    with session.begin_nested():
        audit = _load_audit(session, str(values["idempotency_key"]), lock=True)
        if audit is not None:
            return _idempotent(session, audit, "submit", fingerprint)
        active = _active_request(
            session,
            guild_id=str(values["guild_id"]),
            requester_discord_user_id=str(values["requester_discord_user_id"]),
            lock=True,
        )
        if active is not None:
            raise AccountRegistrationRequestConflictError(
                "registration request conflicts with an active request"
            ) from error
    raise AccountRegistrationRequestConflictError("registration request submission conflicted") from error


def _load_audit(
    session: Session,
    idempotency_key: str,
    *,
    lock: bool = False,
) -> AccountRegistrationOperationAudit | None:
    query = select(AccountRegistrationOperationAudit).where(
        AccountRegistrationOperationAudit.idempotency_key == idempotency_key
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _idempotent(
    session: Session,
    audit: AccountRegistrationOperationAudit,
    action: str,
    fingerprint: str,
) -> AccountRegistrationMutationDTO:
    if audit.action != action or audit.request_fingerprint != fingerprint:
        raise AccountRegistrationRequestConflictError("idempotency key payload does not match the original action")
    request = session.get(AccountRegistrationRequest, audit.account_registration_request_id)
    if request is None:
        raise AccountRegistrationRequestConflictError("registration audit refers to a missing request")
    return AccountRegistrationMutationDTO(action, audit.id, _request_dto(session, request))


def _new_audit(
    *,
    request: AccountRegistrationRequest,
    action: str,
    actor_discord_user_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    before_json: dict[str, object] | None,
    after_json: dict[str, object],
    reason: str | None,
) -> AccountRegistrationOperationAudit:
    return AccountRegistrationOperationAudit(
        account_registration_request_id=request.id,
        action=action,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        before_json=before_json,
        after_json=after_json,
        reason=reason,
    )


def _request_dto(session: Session, request: AccountRegistrationRequest) -> AccountRegistrationRequestDTO:
    persona = session.get(Persona, request.accepted_persona_id) if request.accepted_persona_id is not None else None
    game_account = (
        session.get(GameAccount, request.accepted_game_account_id)
        if request.accepted_game_account_id is not None
        else None
    )
    grant = session.scalar(
        select(CirclePointTransaction.amount).where(
            CirclePointTransaction.idempotency_key == f"account-registration-request-initial-grant:{request.id}"
        )
    )
    return AccountRegistrationRequestDTO(
        id=request.id,
        guild_id=request.guild_id,
        requester_discord_user_id=request.requester_discord_user_id,
        discord_nickname_snapshot=request.discord_nickname_snapshot,
        submitted_uma_pid=request.submitted_uma_pid,
        submitted_nickname=request.submitted_nickname,
        submitted_ingame_name=request.submitted_ingame_name,
        status=request.status,
        reviewed_by_discord_user_id=request.reviewed_by_discord_user_id,
        review_note=request.review_note,
        accepted_persona_id=request.accepted_persona_id,
        accepted_persona_display_name=persona.display_name if persona is not None else None,
        accepted_persona_short_id=persona_short_id(persona.id) if persona is not None else None,
        accepted_game_account_id=request.accepted_game_account_id,
        accepted_game_account_name=game_account.ingame_name if game_account is not None else None,
        initial_grant_amount=int(grant) if grant is not None else 0,
        created_at=database_datetime_as_utc(request.created_at),
        updated_at=database_datetime_as_utc(request.updated_at),
        resolved_at=database_datetime_as_utc(request.resolved_at) if request.resolved_at is not None else None,
    )


def build_account_registration_request_audit_payload(
    request: AccountRegistrationRequest,
) -> dict[str, object]:
    return {
        "id": request.id,
        "guild_id": request.guild_id,
        "requester_discord_user_id": request.requester_discord_user_id,
        "discord_nickname_snapshot": request.discord_nickname_snapshot,
        "submitted_uma_pid": request.submitted_uma_pid,
        "submitted_nickname": request.submitted_nickname,
        "submitted_ingame_name": request.submitted_ingame_name,
        "status": request.status,
        "accepted_persona_id": request.accepted_persona_id,
        "accepted_game_account_id": request.accepted_game_account_id,
        "reviewed_by_discord_user_id": request.reviewed_by_discord_user_id,
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at is not None else None,
    }


def _normalize_submit(command: SubmitAccountRegistrationRequestCommand) -> dict[str, str | None]:
    return {
        "guild_id": _discord_id(command.guild_id, field="guild ID"),
        "requester_discord_user_id": _discord_id(command.requester_discord_user_id, field="requester Discord user ID"),
        "discord_nickname_snapshot": _required_text(
            command.discord_nickname_snapshot, field="Discord nickname", limit=100
        ),
        "submitted_uma_pid": validate_registration_pid(command.submitted_uma_pid),
        "submitted_nickname": _optional_text(command.submitted_nickname, field="nickname", limit=100),
        "submitted_ingame_name": _optional_text(command.submitted_ingame_name, field="ingame name", limit=100),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", limit=128),
    }


def _normalize_cancel(command: CancelAccountRegistrationRequestCommand) -> dict[str, str | int | None]:
    return {
        "request_id": _positive_id(command.request_id, field="request ID"),
        "guild_id": _discord_id(command.guild_id, field="guild ID"),
        "requester_discord_user_id": _discord_id(command.requester_discord_user_id, field="requester Discord user ID"),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", limit=128),
        "reason": _optional_text(command.reason, field="reason", limit=255),
    }


def _normalize_approve(command: ApproveAccountRegistrationRequestCommand) -> dict[str, str | int | None]:
    return {
        "request_id": _positive_id(command.request_id, field="request ID"),
        "reviewed_by_discord_user_id": _discord_id(
            command.reviewed_by_discord_user_id, field="reviewer Discord user ID"
        ),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", limit=128),
        "review_note": _optional_text(command.review_note, field="review note", limit=255),
        "target_persona_id": (
            normalize_persona_id(command.target_persona_id) if command.target_persona_id is not None else None
        ),
    }


def _normalize_reject(command: RejectAccountRegistrationRequestCommand) -> dict[str, str | int]:
    return {
        "request_id": _positive_id(command.request_id, field="request ID"),
        "reviewed_by_discord_user_id": _discord_id(
            command.reviewed_by_discord_user_id, field="reviewer Discord user ID"
        ),
        "idempotency_key": _required_text(command.idempotency_key, field="idempotency key", limit=128),
        "review_note": _required_text(command.review_note, field="review note", limit=255),
    }


def _fingerprint(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _discord_id(value: str, *, field: str) -> str:
    normalized = str(value).strip()
    if not normalized.isascii() or not normalized.isdigit() or len(normalized) > 32:
        raise AccountRegistrationRequestError(f"{field} must be a Discord snowflake")
    return normalized


def _positive_id(value: int, *, field: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise AccountRegistrationRequestError(f"{field} must be positive")
    return value


def _required_text(value: str, *, field: str, limit: int) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > limit:
        raise AccountRegistrationRequestError(f"{field} must be 1..{limit} characters")
    return normalized


def _optional_text(value: str | None, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise AccountRegistrationRequestError(f"{field} must not exceed {limit} characters")
    return normalized
