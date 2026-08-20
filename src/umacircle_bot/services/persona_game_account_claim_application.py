from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256

from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    PersonaLinkOperationAudit,
    RaceEntry,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
    STAFF_SOURCE_GAME_ACCOUNT_CLAIM_TRANSACTION_SOURCE,
    staff_source_game_account_claim_initial_grant_idempotency_key,
)
from umacircle_bot.domain.errors import (
    AccountNotFoundError,
    PersonaLinkConsoleConflictError,
    PersonaLinkConsoleError,
)
from umacircle_bot.domain.personas import persona_short_id
from umacircle_bot.services._legacy_source_account_seed_contract import (
    LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
)
from umacircle_bot.services.accounts import INITIAL_CIRCLE_POINTS
from umacircle_bot.services.application import run_application_command, run_application_query

MAX_SOURCE_ACCOUNT_CHOICES = 25
MAX_CHOICE_LABEL_LENGTH = 100
CLAIM_AUDIT_REASON = "staff confirmed source-only GameAccount claim"
CLAIM_AUDIT_SOURCE = "discord_staff"
CLAIM_AUDIT_ACTION = "game_attach"
CLAIM_OPERATION_LOCK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class StaffSourceGameAccountChoiceDTO:
    value: int
    label: str
    entry_count: int


@dataclass(frozen=True, slots=True)
class StaffSourceGameAccountClaimCommand:
    game_account_id: int
    discord_user_id: str
    discord_nickname: str
    actor_discord_user_id: str
    operation_id: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class StaffSourceGameAccountClaimPreviewDTO:
    game_account_id: int
    game_account_name: str
    identity_status: str
    entry_count: int
    discord_user_id: str
    discord_nickname: str
    persona_state: str
    target_persona_id: str | None
    target_persona_short_id: str | None
    target_display_name: str
    initial_grant_amount: int


@dataclass(frozen=True, slots=True)
class StaffSourceGameAccountClaimResultDTO:
    game_account_id: int
    game_account_name: str
    identity_status: str
    discord_user_id: str
    discord_nickname: str
    target_persona_id: str
    target_persona_short_id: str
    target_display_name: str
    persona_created: bool
    initial_grant_transaction_id: int | None
    initial_grant_amount: int
    audit_id: int
    operation_id: str


def query_staff_source_game_account_choices(
    *,
    query: str = "",
) -> tuple[StaffSourceGameAccountChoiceDTO, ...]:
    return run_application_query(lambda session: search_source_game_accounts_for_claim(session, query=query))


def preview_staff_source_game_account_claim(
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimPreviewDTO:
    return run_application_query(lambda session: preview_source_game_account_claim(session, command=command))


def execute_staff_source_game_account_claim(
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimResultDTO:
    return run_application_command(lambda session: apply_source_game_account_claim(session, command=command))


def search_source_game_accounts_for_claim(
    session: Session,
    *,
    query: str = "",
) -> tuple[StaffSourceGameAccountChoiceDTO, ...]:
    normalized_query = _query_text(query)
    entry_count = (
        select(func.count(RaceEntry.id))
        .where(RaceEntry.game_account_id == GameAccount.id)
        .correlate(GameAccount)
        .scalar_subquery()
    )
    statement = select(
        GameAccount.id,
        GameAccount.nickname,
        GameAccount.ingame_name,
        entry_count.label("entry_count"),
    ).where(
        GameAccount.persona_id.is_(None),
        _source_seed_record_exists(),
    )
    if normalized_query:
        pattern = f"%{normalized_query.casefold()}%"
        statement = statement.where(
            or_(
                cast(GameAccount.id, String).like(pattern),
                func.lower(GameAccount.nickname).like(pattern),
                func.lower(GameAccount.ingame_name).like(pattern),
            )
        )
    rows = session.execute(
        statement.order_by(
            func.lower(func.coalesce(GameAccount.ingame_name, GameAccount.nickname, "")),
            GameAccount.id,
        ).limit(MAX_SOURCE_ACCOUNT_CHOICES)
    ).all()
    return tuple(
        StaffSourceGameAccountChoiceDTO(
            value=int(row.id),
            label=_choice_label(
                account_id=int(row.id),
                display_name=_game_account_name(row.nickname, row.ingame_name, account_id=int(row.id)),
                entry_count=int(row.entry_count or 0),
            ),
            entry_count=int(row.entry_count or 0),
        )
        for row in rows
    )


def preview_source_game_account_claim(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimPreviewDTO:
    normalized = _normalize_command(command)
    game_account = session.get(GameAccount, normalized.game_account_id)
    _validate_source_seed_account(session, game_account)
    discord_account = session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == normalized.discord_user_id)
    )
    if game_account.discord_account_id is not None and (
        discord_account is None or game_account.discord_account_id != discord_account.id
    ):
        raise PersonaLinkConsoleConflictError("source GameAccount has a different legacy Discord account pointer")
    if game_account.persona_id is not None:
        if discord_account is not None and discord_account.persona_id == game_account.persona_id:
            target = session.get(Persona, game_account.persona_id)
            _validate_persona(target)
            return _preview(
                normalized,
                game_account=game_account,
                target=target,
                persona_state="already_claimed",
                entry_count=_entry_count(session, game_account.id),
                initial_grant_amount=_preview_initial_grant_amount(session, target=target),
            )
        raise PersonaLinkConsoleConflictError(
            "source GameAccount already belongs to a Persona; use direct Discord attach or explicit transfer"
        )

    if discord_account is None or discord_account.persona_id is None:
        if discord_account is not None:
            _reject_unowned_legacy_discord_links(session, discord_account=discord_account, game_account=game_account)
        return _preview(
            normalized,
            game_account=game_account,
            target=None,
            persona_state="new_persona",
            entry_count=_entry_count(session, game_account.id),
            initial_grant_amount=INITIAL_CIRCLE_POINTS,
        )

    target = session.get(Persona, discord_account.persona_id)
    _validate_persona(target)
    return _preview(
        normalized,
        game_account=game_account,
        target=target,
        persona_state="existing_persona",
        entry_count=_entry_count(session, game_account.id),
        initial_grant_amount=_preview_initial_grant_amount(session, target=target),
    )


def apply_source_game_account_claim(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimResultDTO:
    normalized = _normalize_command(command)
    with _claim_operation_lock(session, normalized.operation_id):
        try:
            with session.begin_nested():
                return _apply_source_game_account_claim(session, command=normalized)
        except IntegrityError as exc:
            with session.begin_nested():
                audit = _load_audit(session, normalized.operation_id, lock=True)
                if audit is None:
                    raise PersonaLinkConsoleConflictError("concurrent source GameAccount claim conflicted") from exc
                return _idempotent_result(audit, command=normalized)


def _apply_source_game_account_claim(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimResultDTO:
    existing_audit = _load_audit(session, command.operation_id, lock=True)
    if existing_audit is not None:
        return _idempotent_result(existing_audit, command=command)

    observed_account = session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == command.discord_user_id)
    )
    if observed_account is not None and observed_account.persona_id is not None:
        return _claim_into_existing_persona(
            session,
            command=command,
            observed_owner_id=observed_account.persona_id,
        )
    return _claim_with_new_persona(
        session,
        command=command,
        observed_discord_account_id=None if observed_account is None else observed_account.id,
    )


def _claim_into_existing_persona(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
    observed_owner_id: str,
) -> StaffSourceGameAccountClaimResultDTO:
    target = session.scalar(
        select(Persona)
        .where(Persona.id == observed_owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    _validate_persona(target)
    game_account = _lock_game_account(session, command.game_account_id)
    concurrent_audit = _load_audit(session, command.operation_id, lock=True)
    if concurrent_audit is not None:
        return _idempotent_result(concurrent_audit, command=command)
    _validate_source_seed_account(session, game_account)
    if game_account.persona_id not in {None, target.id}:
        raise PersonaLinkConsoleConflictError("source GameAccount owner changed; retry after reviewing the new owner")
    discord_account = session.scalar(
        select(DiscordAccount)
        .where(DiscordAccount.discord_user_id == command.discord_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if discord_account is None or discord_account.persona_id != target.id:
        raise PersonaLinkConsoleConflictError("Discord account Persona owner changed; retry the claim")
    _validate_game_discord_pointer(game_account, discord_account=discord_account)
    wallet = _lock_circle_point_wallet_and_validate_history(session, persona_id=target.id)

    before = _claim_snapshot(
        target=target,
        discord_account=discord_account,
        game_account=game_account,
        wallet=wallet,
        initial_grant=None,
        persona_created=False,
    )
    target.status = "active"
    discord_account.discord_nickname = command.discord_nickname
    game_account.persona_id = target.id
    wallet, initial_grant = _create_wallet_and_initial_grant_if_missing(
        session,
        target=target,
        game_account=game_account,
        actor_discord_user_id=command.actor_discord_user_id,
        wallet=wallet,
    )
    session.flush()
    after = _claim_snapshot(
        target=target,
        discord_account=discord_account,
        game_account=game_account,
        wallet=wallet,
        initial_grant=initial_grant,
        persona_created=False,
    )
    return _write_audit_and_result(
        session,
        command=command,
        target=target,
        discord_account=discord_account,
        game_account=game_account,
        before=before,
        after=after,
        persona_created=False,
        initial_grant=initial_grant,
    )


def _claim_with_new_persona(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
    observed_discord_account_id: int | None,
) -> StaffSourceGameAccountClaimResultDTO:
    game_account = _lock_game_account(session, command.game_account_id)
    concurrent_audit = _load_audit(session, command.operation_id, lock=True)
    if concurrent_audit is not None:
        return _idempotent_result(concurrent_audit, command=command)
    _validate_source_seed_account(session, game_account)
    if game_account.persona_id is not None:
        raise PersonaLinkConsoleConflictError("source GameAccount owner changed; retry after reviewing the new owner")
    discord_account = _lock_or_create_discord_account(
        session,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
    )
    if discord_account.persona_id is not None:
        raise PersonaLinkConsoleConflictError("Discord account Persona owner changed; retry the claim")
    _reject_unowned_legacy_discord_links(
        session,
        discord_account=discord_account,
        game_account=game_account,
    )
    _validate_game_discord_pointer(game_account, discord_account=discord_account)
    before = {
        "persona_created": False,
        "persona": None,
        "discord_account": (
            _discord_snapshot(discord_account, persona_id=None)
            if observed_discord_account_id == discord_account.id
            else None
        ),
        "game_account": _game_snapshot(game_account),
        "circle_point_account": None,
        "initial_grant": None,
    }
    target = Persona(
        display_name=command.discord_nickname,
        display_name_source="discord",
        status="active",
    )
    session.add(target)
    session.flush()
    discord_account.persona_id = target.id
    game_account.persona_id = target.id
    wallet, initial_grant = _create_wallet_and_initial_grant_if_missing(
        session,
        target=target,
        game_account=game_account,
        actor_discord_user_id=command.actor_discord_user_id,
        wallet=None,
    )
    session.flush()
    after = _claim_snapshot(
        target=target,
        discord_account=discord_account,
        game_account=game_account,
        wallet=wallet,
        initial_grant=initial_grant,
        persona_created=True,
    )
    return _write_audit_and_result(
        session,
        command=command,
        target=target,
        discord_account=discord_account,
        game_account=game_account,
        before=before,
        after=after,
        persona_created=True,
        initial_grant=initial_grant,
    )


def _write_audit_and_result(
    session: Session,
    *,
    command: StaffSourceGameAccountClaimCommand,
    target: Persona,
    discord_account: DiscordAccount,
    game_account: GameAccount,
    before: dict[str, object],
    after: dict[str, object],
    persona_created: bool,
    initial_grant: CirclePointTransaction | None,
) -> StaffSourceGameAccountClaimResultDTO:
    audit = PersonaLinkOperationAudit(
        operation_id=command.operation_id,
        action=CLAIM_AUDIT_ACTION,
        actor=f"discord:{command.actor_discord_user_id}",
        reason=_audit_reason(command.note),
        source=CLAIM_AUDIT_SOURCE,
        request_fingerprint=_fingerprint(command),
        target_persona_id=target.id,
        from_persona_id=None,
        discord_account_id=discord_account.id,
        game_account_id=game_account.id,
        before_json=before,
        after_json=after,
    )
    session.add(audit)
    session.flush()
    return StaffSourceGameAccountClaimResultDTO(
        game_account_id=game_account.id,
        game_account_name=_game_account_name(
            game_account.nickname, game_account.ingame_name, account_id=game_account.id
        ),
        identity_status=game_account.identity_status,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
        target_persona_id=target.id,
        target_persona_short_id=persona_short_id(target.id),
        target_display_name=target.display_name,
        persona_created=persona_created,
        initial_grant_transaction_id=None if initial_grant is None else initial_grant.id,
        initial_grant_amount=0 if initial_grant is None else initial_grant.amount,
        audit_id=audit.id,
        operation_id=audit.operation_id,
    )


def _idempotent_result(
    audit: PersonaLinkOperationAudit,
    *,
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimResultDTO:
    if (
        audit.action != CLAIM_AUDIT_ACTION
        or audit.source != CLAIM_AUDIT_SOURCE
        or audit.request_fingerprint != _fingerprint(command)
        or audit.game_account_id != command.game_account_id
        or audit.discord_account_id is None
    ):
        raise PersonaLinkConsoleConflictError("operation ID payload does not match the original source account claim")
    after = audit.after_json
    if not isinstance(after, dict):
        raise PersonaLinkConsoleConflictError("stored source account claim snapshot is invalid")
    persona = after.get("persona")
    game_account = after.get("game_account")
    if not isinstance(persona, dict) or not isinstance(game_account, dict):
        raise PersonaLinkConsoleConflictError("stored source account claim snapshot is invalid")
    persona_id = persona.get("id")
    display_name = persona.get("display_name")
    account_name = game_account.get("display_name")
    identity_status = game_account.get("identity_status")
    persona_created = after.get("persona_created")
    initial_grant = after.get("initial_grant")
    if initial_grant is None:
        initial_grant_transaction_id = None
        initial_grant_amount = 0
    elif isinstance(initial_grant, dict):
        initial_grant_transaction_id = initial_grant.get("id")
        initial_grant_amount = initial_grant.get("amount")
        if (
            not isinstance(initial_grant_transaction_id, int)
            or isinstance(initial_grant_transaction_id, bool)
            or initial_grant_transaction_id <= 0
            or not isinstance(initial_grant_amount, int)
            or isinstance(initial_grant_amount, bool)
            or initial_grant_amount <= 0
        ):
            raise PersonaLinkConsoleConflictError("stored source account claim snapshot is invalid")
    else:
        raise PersonaLinkConsoleConflictError("stored source account claim snapshot is invalid")
    if (
        not isinstance(persona_id, str)
        or not persona_id
        or not isinstance(display_name, str)
        or not display_name
        or not isinstance(account_name, str)
        or not account_name
        or not isinstance(identity_status, str)
        or not identity_status
        or not isinstance(persona_created, bool)
    ):
        raise PersonaLinkConsoleConflictError("stored source account claim snapshot is invalid")
    return StaffSourceGameAccountClaimResultDTO(
        game_account_id=command.game_account_id,
        game_account_name=account_name,
        identity_status=identity_status,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
        target_persona_id=persona_id,
        target_persona_short_id=persona_short_id(persona_id),
        target_display_name=display_name,
        persona_created=persona_created,
        initial_grant_transaction_id=initial_grant_transaction_id,
        initial_grant_amount=initial_grant_amount,
        audit_id=audit.id,
        operation_id=audit.operation_id,
    )


def _preview(
    command: StaffSourceGameAccountClaimCommand,
    *,
    game_account: GameAccount,
    target: Persona | None,
    persona_state: str,
    entry_count: int,
    initial_grant_amount: int,
) -> StaffSourceGameAccountClaimPreviewDTO:
    return StaffSourceGameAccountClaimPreviewDTO(
        game_account_id=game_account.id,
        game_account_name=_game_account_name(
            game_account.nickname, game_account.ingame_name, account_id=game_account.id
        ),
        identity_status=game_account.identity_status,
        entry_count=entry_count,
        discord_user_id=command.discord_user_id,
        discord_nickname=command.discord_nickname,
        persona_state=persona_state,
        target_persona_id=None if target is None else target.id,
        target_persona_short_id=None if target is None else persona_short_id(target.id),
        target_display_name=command.discord_nickname if target is None else target.display_name,
        initial_grant_amount=initial_grant_amount,
    )


def _validate_source_seed_account(session: Session, game_account: GameAccount | None) -> None:
    if game_account is None:
        raise AccountNotFoundError("source GameAccount not found")
    records = tuple(
        session.scalars(
            select(SheetImportRecord.id)
            .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
            .where(
                SheetImportRun.import_kind == LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
                SheetImportRun.status == "completed",
                SheetImportRecord.status == "applied",
                SheetImportRecord.target_entity_type == "game_account",
                SheetImportRecord.target_entity_id == game_account.id,
            )
            .limit(2)
        )
    )
    if len(records) != 1:
        raise PersonaLinkConsoleConflictError("GameAccount is not a uniquely audited source-only account")


def _source_seed_record_exists():
    return (
        select(SheetImportRecord.id)
        .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
        .where(
            SheetImportRun.import_kind == LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
            SheetImportRun.status == "completed",
            SheetImportRecord.status == "applied",
            SheetImportRecord.target_entity_type == "game_account",
            SheetImportRecord.target_entity_id == GameAccount.id,
        )
        .exists()
    )


def _validate_persona(persona: Persona | None) -> None:
    if persona is None:
        raise PersonaLinkConsoleConflictError("Discord account Persona owner is missing")
    if persona.status not in {"active", "inactive"}:
        raise PersonaLinkConsoleConflictError("source GameAccount cannot be claimed by a suspended or archived Persona")


def _reject_unowned_legacy_discord_links(
    session: Session,
    *,
    discord_account: DiscordAccount,
    game_account: GameAccount,
) -> None:
    other = session.scalar(
        select(GameAccount.id)
        .where(
            GameAccount.discord_account_id == discord_account.id,
            GameAccount.id != game_account.id,
        )
        .limit(1)
    )
    if other is not None:
        raise PersonaLinkConsoleConflictError(
            "unlinked Discord account has legacy GameAccount pointers; use explicit identity reconciliation"
        )


def _validate_game_discord_pointer(game_account: GameAccount, *, discord_account: DiscordAccount) -> None:
    if game_account.discord_account_id not in {None, discord_account.id}:
        raise PersonaLinkConsoleConflictError("source GameAccount has a different legacy Discord account pointer")


def _lock_game_account(session: Session, game_account_id: int) -> GameAccount:
    account = session.scalar(
        select(GameAccount)
        .where(GameAccount.id == game_account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise AccountNotFoundError("source GameAccount not found")
    return account


@contextmanager
def _claim_operation_lock(session: Session, operation_id: str) -> Iterator[None]:
    if session.get_bind().dialect.name not in {"mysql", "mariadb"}:
        yield
        return
    lock_digest = sha256(operation_id.encode("utf-8")).hexdigest()[:32]
    lock_name = f"umacircle:source-claim:{lock_digest}"
    acquired = session.scalar(
        text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
        {
            "lock_name": lock_name,
            "timeout_seconds": CLAIM_OPERATION_LOCK_TIMEOUT_SECONDS,
        },
    )
    if acquired != 1:
        raise PersonaLinkConsoleConflictError("could not acquire source GameAccount claim operation lock")
    try:
        yield
    finally:
        session.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})


def _load_audit(
    session: Session,
    operation_id: str,
    *,
    lock: bool,
) -> PersonaLinkOperationAudit | None:
    statement = select(PersonaLinkOperationAudit).where(PersonaLinkOperationAudit.operation_id == operation_id)
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _claim_snapshot(
    *,
    target: Persona,
    discord_account: DiscordAccount,
    game_account: GameAccount,
    wallet: CirclePointAccount | None,
    initial_grant: CirclePointTransaction | None,
    persona_created: bool,
) -> dict[str, object]:
    return {
        "persona_created": persona_created,
        "persona": _persona_snapshot(target),
        "discord_account": _discord_snapshot(discord_account, persona_id=target.id),
        "game_account": _game_snapshot(game_account),
        "circle_point_account": _circle_point_account_snapshot(wallet),
        "initial_grant": _circle_point_transaction_snapshot(initial_grant),
    }


def _persona_snapshot(persona: Persona) -> dict[str, object]:
    return {
        "id": persona.id,
        "display_name": persona.display_name,
        "display_name_source": persona.display_name_source,
        "status": persona.status,
    }


def _circle_point_account_snapshot(account: CirclePointAccount | None) -> dict[str, object] | None:
    if account is None:
        return None
    return {
        "id": account.id,
        "persona_id": account.persona_id,
        "balance": account.balance,
    }


def _circle_point_transaction_snapshot(transaction: CirclePointTransaction | None) -> dict[str, object] | None:
    if transaction is None:
        return None
    return {
        "id": transaction.id,
        "persona_id": transaction.persona_id,
        "game_account_id": transaction.game_account_id,
        "type": transaction.type,
        "amount": transaction.amount,
        "reason": transaction.reason,
        "source": transaction.source,
        "created_by_discord_user_id": transaction.created_by_discord_user_id,
        "idempotency_key": transaction.idempotency_key,
    }


def _preview_initial_grant_amount(session: Session, *, target: Persona) -> int:
    wallet_id = session.scalar(select(CirclePointAccount.id).where(CirclePointAccount.persona_id == target.id))
    if wallet_id is not None:
        return 0
    _reject_missing_wallet_with_point_history(session, persona_id=target.id, lock=False)
    return INITIAL_CIRCLE_POINTS


def _lock_circle_point_wallet_and_validate_history(
    session: Session,
    *,
    persona_id: str,
) -> CirclePointAccount | None:
    wallet = session.scalar(
        select(CirclePointAccount)
        .where(CirclePointAccount.persona_id == persona_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if wallet is None:
        _reject_missing_wallet_with_point_history(session, persona_id=persona_id, lock=True)
    return wallet


def _reject_missing_wallet_with_point_history(session: Session, *, persona_id: str, lock: bool) -> None:
    statement = select(CirclePointTransaction.id).where(CirclePointTransaction.persona_id == persona_id).limit(1)
    if lock:
        statement = statement.with_for_update()
    if session.scalar(statement) is not None:
        raise PersonaLinkConsoleConflictError(
            "Persona has Circle Point history but no wallet; reconcile it before granting initial points"
        )


def _create_wallet_and_initial_grant_if_missing(
    session: Session,
    *,
    target: Persona,
    game_account: GameAccount,
    actor_discord_user_id: str,
    wallet: CirclePointAccount | None,
) -> tuple[CirclePointAccount, CirclePointTransaction | None]:
    if wallet is not None:
        return wallet, None
    wallet = CirclePointAccount(persona_id=target.id, balance=INITIAL_CIRCLE_POINTS)
    initial_grant = CirclePointTransaction(
        persona_id=target.id,
        game_account_id=game_account.id,
        type=ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
        amount=INITIAL_CIRCLE_POINTS,
        reason="initial_circle_points",
        source=STAFF_SOURCE_GAME_ACCOUNT_CLAIM_TRANSACTION_SOURCE,
        created_by_discord_user_id=actor_discord_user_id,
        idempotency_key=staff_source_game_account_claim_initial_grant_idempotency_key(game_account.id),
    )
    session.add_all((wallet, initial_grant))
    session.flush()
    return wallet, initial_grant


def _discord_snapshot(account: DiscordAccount, *, persona_id: str | None) -> dict[str, object]:
    return {
        "id": account.id,
        "discord_user_id": account.discord_user_id,
        "discord_nickname": account.discord_nickname,
        "persona_id": persona_id,
    }


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


def _game_snapshot(account: GameAccount) -> dict[str, object]:
    return {
        "id": account.id,
        "display_name": _game_account_name(account.nickname, account.ingame_name, account_id=account.id),
        "nickname": account.nickname,
        "ingame_name": account.ingame_name,
        "uma_pid": account.uma_pid,
        "identity_status": account.identity_status,
        "persona_id": account.persona_id,
        "discord_account_id": account.discord_account_id,
    }


def _entry_count(session: Session, game_account_id: int) -> int:
    return int(
        session.scalar(select(func.count(RaceEntry.id)).where(RaceEntry.game_account_id == game_account_id)) or 0
    )


def _fingerprint(command: StaffSourceGameAccountClaimCommand) -> str:
    payload = {**asdict(command), "action": CLAIM_AUDIT_ACTION, "source": CLAIM_AUDIT_SOURCE}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _audit_reason(note: str | None) -> str:
    return CLAIM_AUDIT_REASON if note is None else f"{CLAIM_AUDIT_REASON}: {note}"


def _normalize_command(command: StaffSourceGameAccountClaimCommand) -> StaffSourceGameAccountClaimCommand:
    if not isinstance(command, StaffSourceGameAccountClaimCommand):
        raise PersonaLinkConsoleError("staff source GameAccount claim command is required")
    return StaffSourceGameAccountClaimCommand(
        game_account_id=_positive_int(command.game_account_id, field="GameAccount ID"),
        discord_user_id=_discord_user_id(command.discord_user_id),
        discord_nickname=_required_text(command.discord_nickname, field="Discord nickname", maximum=100),
        actor_discord_user_id=_discord_user_id(command.actor_discord_user_id),
        operation_id=_required_text(command.operation_id, field="operation ID", maximum=128),
        note=_optional_text(command.note, field="note", maximum=200),
    )


def _choice_label(*, account_id: int, display_name: str, entry_count: int) -> str:
    suffix = f" · #{account_id} · {entry_count} entries"
    return f"{display_name[: MAX_CHOICE_LABEL_LENGTH - len(suffix)]}{suffix}"


def _game_account_name(nickname: str | None, ingame_name: str | None, *, account_id: int) -> str:
    return _optional_display_text(ingame_name) or _optional_display_text(nickname) or f"GameAccount #{account_id}"


def _optional_display_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _query_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:100]


def _positive_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PersonaLinkConsoleError(f"{field} must be a positive integer")
    return value


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
