from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from umacircle_bot.db.models import DiscordAccount, GameAccount, Persona, PersonaLinkOperationAudit
from umacircle_bot.domain.errors import (
    AccountNotFoundError,
    PersonaLinkConsoleConflictError,
    PersonaLinkConsoleError,
)
from umacircle_bot.domain.personas import normalize_persona_id, persona_short_id
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.persona_link_console import (
    PersonaLinkMutationCommand,
    PersonaLinkMutationDTO,
    apply_persona_link_mutation,
)

MAX_PERSONA_CHOICES = 25
MAX_CHOICE_LABEL_LENGTH = 100
DIRECT_ATTACH_AUDIT_REASON = "staff confirmed direct Discord attach"


@dataclass(frozen=True, slots=True)
class StaffPersonaChoiceDTO:
    value: str
    label: str
    status: str


@dataclass(frozen=True, slots=True)
class StaffDiscordPersonaAttachCommand:
    target_persona_id: str
    discord_user_id: str
    discord_nickname: str
    actor_discord_user_id: str
    operation_id: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class StaffDiscordPersonaAttachPreviewDTO:
    target_persona_id: str
    target_persona_short_id: str
    target_display_name: str
    target_status: str
    discord_user_id: str
    discord_nickname: str
    discord_account_state: str
    activates_persona: bool


@dataclass(frozen=True, slots=True)
class StaffDiscordPersonaAttachResultDTO:
    target_persona_id: str
    target_persona_short_id: str
    target_display_name: str
    target_status: str
    discord_user_id: str
    discord_nickname: str
    audit_id: int
    operation_id: str


def query_staff_persona_choices(*, query: str = "") -> tuple[StaffPersonaChoiceDTO, ...]:
    return run_application_query(lambda session: search_personas_for_discord_attach(session, query=query))


def preview_staff_discord_persona_attach(
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachPreviewDTO:
    return run_application_query(lambda session: preview_discord_persona_attach(session, command=command))


def execute_staff_discord_persona_attach(
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachResultDTO:
    return run_application_command(lambda session: apply_discord_persona_attach(session, command=command))


def search_personas_for_discord_attach(
    session: Session,
    *,
    query: str = "",
) -> tuple[StaffPersonaChoiceDTO, ...]:
    normalized_query = _query_text(query)
    statement = select(Persona.id, Persona.display_name, Persona.status).where(
        Persona.status.in_(("active", "inactive"))
    )
    if normalized_query:
        pattern = f"%{normalized_query.casefold()}%"
        game_match = (
            select(GameAccount.id)
            .where(
                GameAccount.persona_id == Persona.id,
                or_(
                    func.lower(GameAccount.nickname).like(pattern),
                    func.lower(GameAccount.ingame_name).like(pattern),
                ),
            )
            .exists()
        )
        discord_match = (
            select(DiscordAccount.id)
            .where(
                DiscordAccount.persona_id == Persona.id,
                func.lower(DiscordAccount.discord_nickname).like(pattern),
            )
            .exists()
        )
        statement = statement.where(
            or_(
                func.lower(Persona.id).like(pattern),
                func.lower(Persona.display_name).like(pattern),
                game_match,
                discord_match,
            )
        )
    rows = session.execute(
        statement.order_by(
            case((Persona.status == "active", 0), else_=1),
            func.lower(Persona.display_name),
            Persona.id,
        ).limit(MAX_PERSONA_CHOICES)
    ).all()
    return tuple(
        StaffPersonaChoiceDTO(
            value=normalize_persona_id(row.id),
            label=_choice_label(row.display_name, row.id, row.status),
            status=row.status,
        )
        for row in rows
    )


def preview_discord_persona_attach(
    session: Session,
    *,
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachPreviewDTO:
    normalized = _normalize_command(command)
    target = session.get(Persona, normalized.target_persona_id)
    _validate_target(target)
    account = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == normalized.discord_user_id))
    state = "new" if account is None else "unlinked"
    if account is not None and account.persona_id is not None:
        if account.persona_id == target.id:
            state = "already_linked"
        else:
            raise PersonaLinkConsoleConflictError(
                "Discord account is already linked to another Persona; use an explicit transfer workflow"
            )
    return StaffDiscordPersonaAttachPreviewDTO(
        target_persona_id=target.id,
        target_persona_short_id=persona_short_id(target.id),
        target_display_name=target.display_name,
        target_status=target.status,
        discord_user_id=normalized.discord_user_id,
        discord_nickname=normalized.discord_nickname,
        discord_account_state=state,
        activates_persona=target.status == "inactive" and state != "already_linked",
    )


def apply_discord_persona_attach(
    session: Session,
    *,
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachResultDTO:
    normalized = _normalize_command(command)
    with session.begin_nested():
        return _apply_discord_persona_attach(session, command=normalized)


def _apply_discord_persona_attach(
    session: Session,
    *,
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachResultDTO:
    existing_audit = session.scalar(
        select(PersonaLinkOperationAudit).where(PersonaLinkOperationAudit.operation_id == command.operation_id)
    )
    if existing_audit is not None:
        if existing_audit.discord_account_id is None:
            raise PersonaLinkConsoleConflictError("stored Persona link audit has no Discord account")
        account = session.get(DiscordAccount, existing_audit.discord_account_id)
        if account is None:
            raise PersonaLinkConsoleConflictError("stored Persona link audit Discord account is missing")
        mutation = apply_persona_link_mutation(
            session,
            command=_link_mutation_command(command, discord_account_id=account.id),
        )
        return _result(command, mutation=mutation)

    target = session.scalar(
        select(Persona)
        .where(Persona.id == command.target_persona_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    _validate_target(target)
    account = _lock_or_create_discord_account(
        session,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
    )
    mutation = apply_persona_link_mutation(
        session,
        command=_link_mutation_command(command, discord_account_id=account.id),
    )
    return _result(command, mutation=mutation)


def _lock_or_create_discord_account(
    session: Session,
    *,
    discord_user_id: str,
    discord_nickname: str,
) -> DiscordAccount:
    bind = session.get_bind()
    if bind.dialect.name in {"mysql", "mariadb"}:
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
    if account is None:
        account = DiscordAccount(
            discord_user_id=discord_user_id,
            discord_nickname=discord_nickname,
        )
        session.add(account)
        session.flush()
    account.discord_nickname = discord_nickname
    return account


def _link_mutation_command(
    command: StaffDiscordPersonaAttachCommand,
    *,
    discord_account_id: int,
) -> PersonaLinkMutationCommand:
    return PersonaLinkMutationCommand(
        action="discord_attach",
        target_persona_id=command.target_persona_id,
        actor=f"discord:{command.actor_discord_user_id}",
        operation_id=command.operation_id,
        reason=_audit_reason(command.note),
        expected_current_persona_id=None,
        discord_account_id=discord_account_id,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
        mutation_source="discord_staff",
    )


def _result(
    command: StaffDiscordPersonaAttachCommand,
    *,
    mutation: PersonaLinkMutationDTO,
) -> StaffDiscordPersonaAttachResultDTO:
    target = mutation.after.get("target")
    if not isinstance(target, dict):
        raise PersonaLinkConsoleConflictError("stored Persona link audit target snapshot is invalid")
    target_id = target.get("id")
    display_name = target.get("display_name")
    status = target.get("status")
    if not all(isinstance(value, str) and value for value in (target_id, display_name, status)):
        raise PersonaLinkConsoleConflictError("stored Persona link audit target snapshot is invalid")
    return StaffDiscordPersonaAttachResultDTO(
        target_persona_id=target_id,
        target_persona_short_id=persona_short_id(target_id),
        target_display_name=display_name,
        target_status=status,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
        audit_id=mutation.audit_id,
        operation_id=mutation.operation_id,
    )


def _normalize_command(command: StaffDiscordPersonaAttachCommand) -> StaffDiscordPersonaAttachCommand:
    if not isinstance(command, StaffDiscordPersonaAttachCommand):
        raise PersonaLinkConsoleError("staff Discord attach command is required")
    return StaffDiscordPersonaAttachCommand(
        target_persona_id=normalize_persona_id(command.target_persona_id),
        discord_user_id=_discord_user_id(command.discord_user_id),
        discord_nickname=_required_text(command.discord_nickname, field="Discord nickname", maximum=100),
        actor_discord_user_id=_discord_user_id(command.actor_discord_user_id),
        operation_id=_required_text(command.operation_id, field="operation ID", maximum=128),
        note=_optional_text(command.note, field="note", maximum=200),
    )


def _validate_target(target: Persona | None) -> None:
    if target is None:
        raise AccountNotFoundError("target Persona not found")
    if target.status not in {"active", "inactive"}:
        raise PersonaLinkConsoleConflictError("Discord account cannot activate a suspended or archived Persona")


def _audit_reason(note: str | None) -> str:
    return DIRECT_ATTACH_AUDIT_REASON if note is None else f"{DIRECT_ATTACH_AUDIT_REASON}: {note}"


def _choice_label(display_name: str, persona_id: str, status: str) -> str:
    safe_name = " ".join(str(display_name).split()) or "Persona"
    suffix = f" · {persona_short_id(persona_id)} · {status}"
    return f"{safe_name[: MAX_CHOICE_LABEL_LENGTH - len(suffix)]}{suffix}"


def _query_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:100]


def _discord_user_id(value: str) -> str:
    normalized = _required_text(value, field="Discord user ID", maximum=32)
    if not normalized.isascii() or not normalized.isdigit() or int(normalized) <= 0:
        raise PersonaLinkConsoleError("Discord user ID must be a positive decimal snowflake")
    return normalized


def _required_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PersonaLinkConsoleError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise PersonaLinkConsoleError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersonaLinkConsoleError(f"{field} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    return _required_text(normalized, field=field, maximum=maximum)
