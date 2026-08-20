from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    AccountRegistrationOperationAudit,
    AccountRegistrationRequest,
    CirclePointTransaction,
    GameAccount,
    Persona,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
    account_registration_initial_grant_idempotency_key,
)
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.registration_requests import build_account_registration_request_audit_payload
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayParticipant


def registration_provenance_matches(
    session: Session,
    *,
    participant: CurrentWin5ReplayParticipant,
    persona: Persona,
    account: GameAccount,
) -> bool:
    if (
        participant.registration_guild_id is None
        or participant.registration_approved_by_discord_user_id is None
        or participant.registration_target_initial_grant is None
    ):
        return False
    requests = tuple(
        session.scalars(
            select(AccountRegistrationRequest).where(
                AccountRegistrationRequest.requester_discord_user_id == participant.discord_user_id,
                AccountRegistrationRequest.submitted_uma_pid == participant.uma_pid,
                AccountRegistrationRequest.status == "approved",
            )
        )
    )
    if len(requests) != 1:
        return False
    request = requests[0]
    if (
        request.guild_id != participant.registration_guild_id
        or request.discord_nickname_snapshot != participant.discord_nickname
        or request.submitted_nickname != participant.nickname
        or request.submitted_ingame_name != participant.ingame_name
        or request.reviewed_by_discord_user_id != participant.registration_approved_by_discord_user_id
        or request.accepted_persona_id != persona.id
        or request.accepted_game_account_id != account.id
        or request.active_request_marker is not None
        or request.resolved_at is None
        or account.nickname != participant.nickname
        or account.ingame_name != participant.ingame_name
    ):
        return False

    audits = tuple(
        session.scalars(
            select(AccountRegistrationOperationAudit)
            .where(AccountRegistrationOperationAudit.account_registration_request_id == request.id)
            .order_by(AccountRegistrationOperationAudit.id)
        )
    )
    audit_by_action = {audit.action: audit for audit in audits}
    submit = audit_by_action.get("submit")
    approve = audit_by_action.get("approve")
    if len(audits) != 2 or submit is None or approve is None:
        return False
    current_payload = build_account_registration_request_audit_payload(request)
    approved_payload = approve.after_json if isinstance(approve.after_json, dict) else None
    pending_payload = submit.after_json if isinstance(submit.after_json, dict) else None
    if (
        submit.actor_discord_user_id != participant.discord_user_id
        or submit.idempotency_key != request.idempotency_key
        or submit.request_fingerprint != request.request_fingerprint
        or submit.before_json is not None
        or approve.actor_discord_user_id != participant.registration_approved_by_discord_user_id
        or approve.before_json != pending_payload
        or approve.reason != request.review_note
        or not _registration_audit_payload_matches(
            pending_payload,
            request=request,
            status="pending",
            accepted_persona_id=None,
            accepted_game_account_id=None,
            reviewed_by_discord_user_id=None,
            resolved_at=None,
        )
        or not _registration_audit_payload_matches(
            approved_payload,
            request=request,
            status="approved",
            accepted_persona_id=persona.id,
            accepted_game_account_id=account.id,
            reviewed_by_discord_user_id=participant.registration_approved_by_discord_user_id,
            resolved_at=request.resolved_at,
        )
        or not _same_registration_payload(current_payload, approved_payload)
    ):
        return False

    grants = tuple(
        session.scalars(
            select(CirclePointTransaction).where(
                CirclePointTransaction.idempotency_key == account_registration_initial_grant_idempotency_key(request.id)
            )
        )
    )
    if len(grants) != 1:
        return False
    grant = grants[0]
    return (
        grant.persona_id == persona.id
        and grant.game_account_id == account.id
        and grant.type == ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE
        and grant.source == ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE
        and grant.amount == participant.registration_target_initial_grant
        and grant.reason == "initial_room_points"
        and grant.created_by_discord_user_id == participant.registration_approved_by_discord_user_id
    )


def _registration_audit_payload_matches(
    payload: object,
    *,
    request: AccountRegistrationRequest,
    status: str,
    accepted_persona_id: str | None,
    accepted_game_account_id: int | None,
    reviewed_by_discord_user_id: str | None,
    resolved_at: datetime | None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    expected = {
        "id": request.id,
        "guild_id": request.guild_id,
        "requester_discord_user_id": request.requester_discord_user_id,
        "discord_nickname_snapshot": request.discord_nickname_snapshot,
        "submitted_uma_pid": request.submitted_uma_pid,
        "submitted_nickname": request.submitted_nickname,
        "submitted_ingame_name": request.submitted_ingame_name,
        "status": status,
        "accepted_persona_id": accepted_persona_id,
        "accepted_game_account_id": accepted_game_account_id,
        "reviewed_by_discord_user_id": reviewed_by_discord_user_id,
    }
    if {key: payload.get(key) for key in expected} != expected:
        return False
    value = payload.get("resolved_at")
    if resolved_at is None:
        return value is None
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return database_datetime_as_utc(parsed).replace(microsecond=0) == database_datetime_as_utc(resolved_at).replace(
        microsecond=0
    )


def _same_registration_payload(current: dict[str, object], approved: object) -> bool:
    if not isinstance(approved, dict):
        return False
    keys = set(current) - {"resolved_at"}
    return keys == set(approved) - {"resolved_at"} and all(current[key] == approved[key] for key in keys)
