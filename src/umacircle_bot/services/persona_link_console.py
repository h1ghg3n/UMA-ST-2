from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import DiscordAccount, GameAccount, Persona, PersonaLinkOperationAudit
from umacircle_bot.domain.errors import (
    AccountNotFoundError,
    PersonaLinkConsoleConflictError,
    PersonaLinkConsoleError,
)
from umacircle_bot.domain.personas import normalize_persona_id
from umacircle_bot.services.transitional_main_game_account import (
    clear_transitional_main_game_account_pointer,
)

PERSONA_LINK_SOURCE = "server_console"
PERSONA_LINK_SOURCES = frozenset({PERSONA_LINK_SOURCE, "discord_staff"})
_ACCOUNT_ACTIONS = {
    "discord_attach",
    "discord_detach",
    "discord_transfer",
    "game_attach",
    "game_detach",
    "game_transfer",
}
_RETIRED_ACTIONS = {"set_main_game_account"}
_ACTIONS = _ACCOUNT_ACTIONS | {"set_display_name", "restore_display_name_source"}


@dataclass(frozen=True, slots=True)
class PersonaLinkMutationCommand:
    action: str
    target_persona_id: str
    actor: str
    operation_id: str
    reason: str
    expected_current_persona_id: str | None = None
    discord_account_id: int | None = None
    game_account_id: int | None = None
    # Retained only so retries can reproduce fingerprints written before main/sub retirement.
    expected_main_game_account_id: int | None = None
    replacement_main_game_account_id: int | None = None
    allow_orphan: bool = False
    display_name: str | None = None
    # Optional immutable Discord identity input used by bounded staff adapters.
    # Omitted legacy console payloads keep their historical fingerprint shape.
    discord_user_id: str | None = None
    discord_nickname: str | None = None
    mutation_source: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaLinkInspectionDTO:
    persona: dict[str, object]


@dataclass(frozen=True, slots=True)
class PersonaLinkMutationDTO:
    action: str
    audit_id: int
    operation_id: str
    target_persona_id: str
    before: dict[str, object]
    after: dict[str, object]


def inspect_persona_link(session: Session, *, persona_id: str) -> PersonaLinkInspectionDTO:
    persona = _load_persona(session, normalize_persona_id(persona_id), lock=False)
    if persona is None:
        raise AccountNotFoundError("Persona not found")
    return PersonaLinkInspectionDTO(persona=_persona_snapshot(session, persona))


def preview_persona_link_mutation(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
) -> PersonaLinkMutationDTO:
    """Validate and render an operation without persisting a link or audit record."""
    normalized = _normalize_command(command)
    _ensure_application_transaction(session)
    with session.begin_nested() as savepoint:
        result = _apply(session, command=normalized, write_audit=False)
        savepoint.rollback()
    return result


def apply_persona_link_mutation(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
) -> PersonaLinkMutationDTO:
    normalized = _normalize_command(command)
    _ensure_application_transaction(session)
    try:
        with session.begin_nested():
            return _apply(session, command=normalized, write_audit=True)
    except IntegrityError as exc:
        return _recover_concurrent_retry(session, command=normalized, error=exc)


def _apply(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
    write_audit: bool,
) -> PersonaLinkMutationDTO:
    existing = _load_audit(session, command.operation_id, lock=write_audit)
    if existing is not None:
        return _idempotent_result(existing, command=command)
    _reject_retired_main_account_mutation(command)

    target = _load_persona(session, command.target_persona_id, lock=True)
    if target is None:
        raise AccountNotFoundError("target Persona not found")
    account, actual_owner = _load_action_account(session, command=command)
    _validate_expected_owner(command, actual_owner)
    source = _load_persona(session, actual_owner, lock=True) if actual_owner is not None else None
    if actual_owner is not None and source is None:
        raise PersonaLinkConsoleConflictError("current account owner disappeared during operation")

    before = _snapshots(session, target=target, source=source)
    _apply_action(session, command=command, target=target, source=source, account=account)
    # Runtime sessions disable autoflush. Persist the ownership mutation before
    # querying whether either Persona still has a linked Discord account.
    session.flush()
    _inactivate_orphans(session, target, source)
    session.flush()
    after = _snapshots(session, target=target, source=source)
    if not write_audit:
        return PersonaLinkMutationDTO(
            action=command.action,
            audit_id=0,
            operation_id=command.operation_id,
            target_persona_id=target.id,
            before=before,
            after=after,
        )

    audit = PersonaLinkOperationAudit(
        operation_id=command.operation_id,
        action=command.action,
        actor=command.actor,
        reason=command.reason,
        source=_mutation_source(command),
        request_fingerprint=_fingerprint(command),
        target_persona_id=target.id,
        from_persona_id=source.id if source is not None else None,
        discord_account_id=command.discord_account_id,
        game_account_id=command.game_account_id,
        before_json=before,
        after_json=after,
    )
    session.add(audit)
    session.flush()
    return PersonaLinkMutationDTO(
        action=command.action,
        audit_id=audit.id,
        operation_id=command.operation_id,
        target_persona_id=target.id,
        before=before,
        after=after,
    )


def _apply_action(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
    target: Persona,
    source: Persona | None,
    account: DiscordAccount | GameAccount | None,
) -> None:
    if command.action.startswith("discord_"):
        if not isinstance(account, DiscordAccount):
            raise AssertionError("discord operation did not load a Discord account")
        _apply_discord_action(command, target=target, source=source, account=account)
        return
    if command.action.startswith("game_"):
        if not isinstance(account, GameAccount):
            raise AssertionError("game operation did not load a game account")
        _apply_game_action(command, target=target, source=source, account=account)
        return
    if command.action == "set_display_name":
        target.display_name = _text(command.display_name, field="display name", maximum=100)
        target.display_name_source = "manual"
        return
    if command.action == "restore_display_name_source":
        display_name, source_name = _restored_display_name(session, target)
        target.display_name = display_name
        target.display_name_source = source_name
        return
    raise AssertionError(f"unsupported Persona link action: {command.action}")


def _apply_discord_action(
    command: PersonaLinkMutationCommand,
    *,
    target: Persona,
    source: Persona | None,
    account: DiscordAccount,
) -> None:
    if command.action == "discord_attach":
        if source is not None:
            raise PersonaLinkConsoleConflictError("Discord account is already linked; use transfer")
        _activate_discord_target(target)
        account.persona_id = target.id
        return
    if command.action == "discord_detach":
        if source is None or source.id != target.id:
            raise PersonaLinkConsoleConflictError("Discord account is not linked to the target Persona")
        account.persona_id = None
        return
    if command.action == "discord_transfer":
        if source is None:
            raise PersonaLinkConsoleConflictError("Discord account is unlinked; use attach")
        if source.id == target.id:
            raise PersonaLinkConsoleConflictError("Discord account already belongs to the target Persona")
        _activate_discord_target(target)
        account.persona_id = target.id
        return
    raise AssertionError(f"unsupported Discord action: {command.action}")


def _activate_discord_target(target: Persona) -> None:
    if target.status not in {"active", "inactive"}:
        raise PersonaLinkConsoleConflictError("Discord account cannot activate a suspended or archived Persona")
    target.status = "active"


def _apply_game_action(
    command: PersonaLinkMutationCommand,
    *,
    target: Persona,
    source: Persona | None,
    account: GameAccount,
) -> None:
    if command.action == "game_attach":
        if source is not None:
            raise PersonaLinkConsoleConflictError("game account is already linked; use transfer")
        account.persona_id = target.id
        return
    if command.action == "game_detach":
        if source is None or source.id != target.id:
            raise PersonaLinkConsoleConflictError("game account is not linked to the target Persona")
        clear_transitional_main_game_account_pointer(
            persona=source,
            removed_game_account_id=account.id,
        )
        account.persona_id = None
        return
    if command.action == "game_transfer":
        if source is None:
            raise PersonaLinkConsoleConflictError("game account is unlinked; use attach")
        if source.id == target.id:
            raise PersonaLinkConsoleConflictError("game account already belongs to the target Persona")
        clear_transitional_main_game_account_pointer(
            persona=source,
            removed_game_account_id=account.id,
        )
        account.persona_id = target.id
        return
    raise AssertionError(f"unsupported game action: {command.action}")


def _inactivate_orphans(session: Session, *personas: Persona | None) -> None:
    for persona in {item.id: item for item in personas if item is not None}.values():
        if persona.status != "active":
            continue
        discord_count = session.scalar(
            select(DiscordAccount.id).where(DiscordAccount.persona_id == persona.id).limit(1)
        )
        if discord_count is None:
            persona.status = "inactive"


def _restored_display_name(session: Session, persona: Persona) -> tuple[str, str]:
    discord = session.scalar(
        select(DiscordAccount)
        .where(DiscordAccount.persona_id == persona.id)
        .order_by(DiscordAccount.id)
        .with_for_update()
    )
    if discord is None:
        raise PersonaLinkConsoleError("cannot restore Persona display name without a linked Discord account")
    return _text(discord.discord_nickname, field="Discord nickname", maximum=100), "discord"


def _load_action_account(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
) -> tuple[DiscordAccount | GameAccount | None, str | None]:
    if command.action.startswith("discord_"):
        account = _load_discord_account(session, command.discord_account_id, lock=True)
        if account is None:
            raise AccountNotFoundError("Discord account not found")
        return account, account.persona_id
    if command.action.startswith("game_"):
        account = _load_game_account(session, command.game_account_id, lock=True)
        if account is None:
            raise AccountNotFoundError("game account not found")
        return account, account.persona_id
    return None, None


def _validate_expected_owner(command: PersonaLinkMutationCommand, actual_owner: str | None) -> None:
    if command.action not in _ACCOUNT_ACTIONS:
        return
    if actual_owner != command.expected_current_persona_id:
        expected = command.expected_current_persona_id or "unlinked"
        actual = actual_owner or "unlinked"
        raise PersonaLinkConsoleConflictError(f"account current owner changed; expected {expected}, current {actual}")


def _snapshots(session: Session, *, target: Persona, source: Persona | None) -> dict[str, object]:
    return {
        "target": _persona_snapshot(session, target),
        "from": None if source is None or source.id == target.id else _persona_snapshot(session, source),
    }


def _persona_snapshot(session: Session, persona: Persona) -> dict[str, object]:
    discord_accounts = session.scalars(
        select(DiscordAccount).where(DiscordAccount.persona_id == persona.id).order_by(DiscordAccount.id)
    ).all()
    game_accounts = session.scalars(
        select(GameAccount).where(GameAccount.persona_id == persona.id).order_by(GameAccount.id)
    ).all()
    return {
        "id": persona.id,
        "display_name": persona.display_name,
        "display_name_source": persona.display_name_source,
        "status": persona.status,
        "discord_accounts": [
            {"id": item.id, "discord_user_id": item.discord_user_id, "discord_nickname": item.discord_nickname}
            for item in discord_accounts
        ],
        "game_accounts": [
            {
                "id": item.id,
                "discord_account_id": item.discord_account_id,
                "uma_pid": item.uma_pid,
                "nickname": item.nickname,
                "ingame_name": item.ingame_name,
                "identity_status": item.identity_status,
            }
            for item in game_accounts
        ],
    }


def _normalize_command(command: PersonaLinkMutationCommand) -> PersonaLinkMutationCommand:
    action = _text(command.action, field="action", maximum=32)
    if action not in _ACTIONS | _RETIRED_ACTIONS:
        raise PersonaLinkConsoleError("unsupported Persona link-console action")
    expected_owner = (
        normalize_persona_id(command.expected_current_persona_id)
        if command.expected_current_persona_id is not None
        else None
    )
    normalized = PersonaLinkMutationCommand(
        action=action,
        target_persona_id=normalize_persona_id(command.target_persona_id),
        actor=_text(command.actor, field="actor", maximum=100),
        operation_id=_text(command.operation_id, field="operation ID", maximum=128),
        reason=_text(command.reason, field="audit reason", maximum=255),
        expected_current_persona_id=expected_owner,
        discord_account_id=_positive_int(command.discord_account_id, field="Discord account ID")
        if command.discord_account_id is not None
        else None,
        game_account_id=_positive_int(command.game_account_id, field="game account ID")
        if command.game_account_id is not None
        else None,
        expected_main_game_account_id=_positive_int(
            command.expected_main_game_account_id, field="expected main game account ID"
        )
        if command.expected_main_game_account_id is not None
        else None,
        replacement_main_game_account_id=_positive_int(
            command.replacement_main_game_account_id,
            field="replacement main game account ID",
        )
        if command.replacement_main_game_account_id is not None
        else None,
        allow_orphan=command.allow_orphan,
        display_name=command.display_name,
        discord_user_id=(_discord_user_id(command.discord_user_id) if command.discord_user_id is not None else None),
        discord_nickname=(
            _text(command.discord_nickname, field="Discord nickname", maximum=100)
            if command.discord_nickname is not None
            else None
        ),
        mutation_source=(
            _text(command.mutation_source, field="mutation source", maximum=32)
            if command.mutation_source is not None
            else None
        ),
    )
    if not isinstance(command.allow_orphan, bool):
        raise PersonaLinkConsoleError("allow orphan must be true or false")
    if action.startswith("discord_") and normalized.discord_account_id is None:
        raise PersonaLinkConsoleError("Discord account ID is required for this action")
    if (action.startswith("game_") or action in _RETIRED_ACTIONS) and normalized.game_account_id is None:
        raise PersonaLinkConsoleError("game account ID is required for this action")
    if action == "set_display_name" and command.display_name is None:
        raise PersonaLinkConsoleError("display name is required when setting a Persona display name")
    if action != "set_display_name" and command.display_name is not None:
        raise PersonaLinkConsoleError("display name is only valid for set_display_name")
    if (normalized.discord_user_id is None) != (normalized.discord_nickname is None):
        raise PersonaLinkConsoleError("Discord user ID and nickname metadata must be provided together")
    if normalized.discord_user_id is not None and action != "discord_attach":
        raise PersonaLinkConsoleError("Discord identity metadata is only valid for discord_attach")
    if normalized.mutation_source is not None and normalized.mutation_source not in PERSONA_LINK_SOURCES:
        raise PersonaLinkConsoleError("unsupported Persona link mutation source")
    return normalized


def _reject_retired_main_account_mutation(command: PersonaLinkMutationCommand) -> None:
    if (
        command.action in _RETIRED_ACTIONS
        or command.expected_main_game_account_id is not None
        or command.replacement_main_game_account_id is not None
        or command.allow_orphan
    ):
        raise PersonaLinkConsoleError(
            "main game account mutations are retired; only an exact retry of an existing audit is supported"
        )


def _load_persona(session: Session, persona_id: str, *, lock: bool) -> Persona | None:
    query = select(Persona).where(Persona.id == persona_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _load_discord_account(session: Session, account_id: int | None, *, lock: bool) -> DiscordAccount | None:
    if account_id is None:
        return None
    query = select(DiscordAccount).where(DiscordAccount.id == account_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _load_game_account(session: Session, account_id: int | None, *, lock: bool) -> GameAccount | None:
    if account_id is None:
        return None
    query = select(GameAccount).where(GameAccount.id == account_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _load_audit(session: Session, operation_id: str, *, lock: bool) -> PersonaLinkOperationAudit | None:
    query = select(PersonaLinkOperationAudit).where(PersonaLinkOperationAudit.operation_id == operation_id)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _idempotent_result(
    audit: PersonaLinkOperationAudit,
    *,
    command: PersonaLinkMutationCommand,
) -> PersonaLinkMutationDTO:
    if audit.request_fingerprint != _fingerprint(command):
        raise PersonaLinkConsoleConflictError("operation ID payload does not match the original Persona link operation")
    return PersonaLinkMutationDTO(
        action=audit.action,
        audit_id=audit.id,
        operation_id=audit.operation_id,
        target_persona_id=audit.target_persona_id,
        before=_json_object(audit.before_json, field="before"),
        after=_json_object(audit.after_json, field="after"),
    )


def _recover_concurrent_retry(
    session: Session,
    *,
    command: PersonaLinkMutationCommand,
    error: IntegrityError,
) -> PersonaLinkMutationDTO:
    with session.begin_nested():
        audit = _load_audit(session, command.operation_id, lock=True)
        if audit is None:
            raise PersonaLinkConsoleConflictError("concurrent Persona link operation conflicted") from error
        return _idempotent_result(audit, command=command)


def _ensure_application_transaction(session: Session) -> None:
    if not session.in_transaction():
        session.begin()
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _fingerprint(command: PersonaLinkMutationCommand) -> str:
    payload = asdict(command)
    for compatibility_field in ("discord_user_id", "discord_nickname", "mutation_source"):
        if payload[compatibility_field] is None:
            payload.pop(compatibility_field)
    payload["source"] = payload.pop("mutation_source", None) or PERSONA_LINK_SOURCE
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: str | None, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PersonaLinkConsoleError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise PersonaLinkConsoleError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _positive_int(value: int | None, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PersonaLinkConsoleError(f"{field} must be a positive integer")
    return value


def _discord_user_id(value: str) -> str:
    if not isinstance(value, str):
        raise PersonaLinkConsoleError("Discord user ID must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 32 or not normalized.isascii() or not normalized.isdigit():
        raise PersonaLinkConsoleError("Discord user ID must contain 1 to 32 ASCII digits")
    return normalized


def _mutation_source(command: PersonaLinkMutationCommand) -> str:
    return command.mutation_source or PERSONA_LINK_SOURCE


def _json_object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PersonaLinkConsoleConflictError(f"stored Persona link audit {field} snapshot is invalid")
    return value
