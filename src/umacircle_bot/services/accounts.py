from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text
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
    RatingEvent,
    Win5Entry,
)
from umacircle_bot.domain.errors import (
    AccountAlreadyRegisteredError,
    AccountNotFoundError,
    DuplicateUmaPidError,
    IdentityStateError,
)
from umacircle_bot.domain.identity import IdentityStatus, normalize_uma_pid, validate_registration_pid
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import AccountInfoDTO, GameAccountDTO, GameAccountRegistrationDTO
from umacircle_bot.services.persona_wallets import lock_persona_wallet_for_game_account
from umacircle_bot.services.player_link_requests import assert_no_active_player_link_request_for_registration

INITIAL_CIRCLE_POINTS = 500


def register_game_account(
    session: Session,
    *,
    discord_user_id: str,
    discord_nickname: str,
    uma_pid: str,
    nickname: str | None = None,
    ingame_name: str | None = None,
) -> GameAccountRegistrationDTO:
    normalized_pid = validate_registration_pid(uma_pid)
    _validate_registration_name(nickname, field_name="nickname")
    _validate_registration_name(ingame_name, field_name="ingame_name")
    _begin_sqlite_outer_transaction_if_needed(session)
    grant_amount = INITIAL_CIRCLE_POINTS
    with _pid_registration_lock(session, normalized_pid):
        with session.begin_nested():
            discord_account = _lock_or_create_discord_account(
                session,
                discord_user_id=discord_user_id,
                discord_nickname=discord_nickname,
            )
            assert_no_active_player_link_request_for_registration(
                session,
                requester_discord_user_id=discord_account.discord_user_id,
            )
            cancelled = session.scalar(
                select(GameAccount).where(GameAccount.discord_account_id == discord_account.id).with_for_update()
            )
            if cancelled is not None and cancelled.identity_status == IdentityStatus.CANCELLED.value:
                registration = _reactivate_cancelled_game_account(
                    session,
                    game_account=cancelled,
                    uma_pid=normalized_pid,
                    nickname=nickname,
                    ingame_name=ingame_name,
                    discord_user_id=discord_account.discord_user_id,
                )
                discord_account.discord_nickname = discord_nickname
                session.flush()
                return registration
            _raise_existing_registration_conflict(
                session,
                discord_account_id=discord_account.id,
                uma_pid=normalized_pid,
            )
            registration = _create_registered_game_account(
                session,
                discord_account=discord_account,
                uma_pid=normalized_pid,
                nickname=nickname,
                ingame_name=ingame_name,
                grant_amount=grant_amount,
            )
            # A rejected registration must never change a previously stored nickname.
            discord_account.discord_nickname = discord_nickname
            session.flush()
    return registration


def cancel_owned_registration(session: Session, *, discord_user_id: str) -> GameAccountRegistrationDTO:
    """Cancel only a pristine self-registration while retaining an audit ledger."""
    with session.begin_nested():
        account = session.scalar(
            select(GameAccount)
            .join(DiscordAccount, GameAccount.discord_account_id == DiscordAccount.id)
            .where(DiscordAccount.discord_user_id == discord_user_id)
            .with_for_update()
        )
        if account is None:
            raise AccountNotFoundError("registered game account not found")
        if account.identity_status != IdentityStatus.CONFIRMED.value:
            raise IdentityStateError("only a newly confirmed registration can be cancelled")
        if account.persona_id is None:
            raise IdentityStateError("registered game account has no Persona owner")
        _locked_account, point_account = lock_persona_wallet_for_game_account(session, game_account_id=account.id)
        transactions = list(
            session.scalars(
                select(CirclePointTransaction)
                .where(CirclePointTransaction.game_account_id == account.id)
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if (
            point_account is None
            or point_account.balance != INITIAL_CIRCLE_POINTS
            or len(transactions) != 1
            or transactions[0].type != "initial_grant"
            or transactions[0].amount != INITIAL_CIRCLE_POINTS
            or transactions[0].source != "account_registration"
            or session.scalar(select(RatingEvent.id).where(RatingEvent.game_account_id == account.id).limit(1))
            is not None
            or session.scalar(select(Win5Entry.id).where(Win5Entry.game_account_id == account.id).limit(1)) is not None
        ):
            raise IdentityStateError("registration can be cancelled only before any operational activity")
        point_account.balance = 0
        session.add(
            CirclePointTransaction(
                persona_id=account.persona_id,
                game_account_id=account.id,
                type="registration_cancel",
                amount=-INITIAL_CIRCLE_POINTS,
                reason="account_registration_cancelled",
                source="account_registration_cancel",
                created_by_discord_user_id=discord_user_id,
                idempotency_key=f"account-registration-cancel:{account.id}",
            )
        )
        account.uma_pid = None
        account.nickname = None
        account.ingame_name = None
        account.identity_status = IdentityStatus.CANCELLED.value
        session.flush()
    return _registration_dto(
        account, point_account, initial_grant_transaction=None, initial_grant_amount=0, was_created=False
    )


def _reactivate_cancelled_game_account(
    session: Session,
    *,
    game_account: GameAccount,
    uma_pid: str,
    nickname: str | None,
    ingame_name: str | None,
    discord_user_id: str,
) -> GameAccountRegistrationDTO:
    duplicate = session.scalar(select(GameAccount).where(GameAccount.uma_pid == uma_pid).with_for_update())
    if duplicate is not None:
        raise DuplicateUmaPidError("uma_pid is already registered")
    if game_account.persona_id is None:
        raise IdentityStateError("cancelled registration has no Persona owner")
    _locked_account, point_account = lock_persona_wallet_for_game_account(session, game_account_id=game_account.id)
    if point_account.balance != 0:
        raise IdentityStateError("cancelled registration has an invalid point balance")
    game_account.uma_pid = uma_pid
    game_account.nickname = nickname
    game_account.ingame_name = ingame_name
    game_account.identity_status = IdentityStatus.CONFIRMED.value
    point_account.balance = INITIAL_CIRCLE_POINTS
    transaction = CirclePointTransaction(
        persona_id=game_account.persona_id,
        game_account_id=game_account.id,
        type="initial_grant",
        amount=INITIAL_CIRCLE_POINTS,
        reason="account_registration_reactivated",
        source="account_registration",
        created_by_discord_user_id=discord_user_id,
        idempotency_key=f"account-registration-reactivation:{game_account.id}",
    )
    session.add(transaction)
    session.flush()
    return _registration_dto(
        game_account,
        point_account,
        initial_grant_transaction=transaction,
        initial_grant_amount=INITIAL_CIRCLE_POINTS,
        was_created=True,
    )


def _lock_or_create_discord_account(
    session: Session,
    *,
    discord_user_id: str,
    discord_nickname: str,
) -> DiscordAccount:
    if _uses_mysql_upsert(session):
        session.execute(
            mysql_insert(DiscordAccount)
            .values(discord_user_id=discord_user_id, discord_nickname=discord_nickname)
            .on_duplicate_key_update(id=DiscordAccount.id)
        )
        discord_account = session.scalar(
            select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id).with_for_update()
        )
        if discord_account is None:
            raise IdentityStateError("Discord account could not be locked")
        return discord_account

    discord_account = session.scalar(
        select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id).with_for_update()
    )
    if discord_account is not None:
        return discord_account
    discord_account = DiscordAccount(
        discord_user_id=discord_user_id,
        discord_nickname=discord_nickname,
    )
    session.add(discord_account)
    session.flush()
    return discord_account


def _create_registered_game_account(
    session: Session,
    *,
    discord_account: DiscordAccount,
    uma_pid: str,
    nickname: str | None,
    ingame_name: str | None,
    grant_amount: int,
) -> GameAccountRegistrationDTO:
    persona = Persona(
        display_name=discord_account.discord_nickname,
        display_name_source="discord",
        status="active",
    )
    session.add(persona)
    session.flush()
    discord_account.persona_id = persona.id
    game_account = _insert_game_account(
        session,
        discord_account_id=discord_account.id,
        persona_id=persona.id,
        uma_pid=uma_pid,
        nickname=nickname,
        ingame_name=ingame_name,
    )

    point_account = CirclePointAccount(persona_id=persona.id, balance=grant_amount)
    session.add(point_account)
    session.flush()
    initial_grant = CirclePointTransaction(
        persona_id=persona.id,
        game_account_id=game_account.id,
        type="initial_grant",
        amount=grant_amount,
        reason="initial_room_points",
        source="account_registration",
        created_by_discord_user_id=discord_account.discord_user_id,
        idempotency_key=f"account-registration-initial-grant:{discord_account.discord_user_id}",
    )
    session.add(initial_grant)
    session.flush()

    return _registration_dto(
        game_account,
        point_account,
        initial_grant_transaction=initial_grant,
        initial_grant_amount=grant_amount,
        was_created=True,
    )


def _insert_game_account(
    session: Session,
    *,
    discord_account_id: int,
    persona_id: str,
    uma_pid: str,
    nickname: str | None,
    ingame_name: str | None,
) -> GameAccount:
    values = {
        "discord_account_id": discord_account_id,
        "persona_id": persona_id,
        "uma_pid": uma_pid,
        "nickname": nickname,
        "ingame_name": ingame_name,
        "identity_status": IdentityStatus.CONFIRMED.value,
    }
    game_account = GameAccount(**values)
    try:
        with session.begin_nested():
            session.add(game_account)
            session.flush()
    except IntegrityError as exc:
        if not _is_mariadb_duplicate_key_error(exc):
            raise
        _raise_existing_registration_conflict(
            session,
            discord_account_id=discord_account_id,
            uma_pid=uma_pid,
            original_error=exc,
        )
    return game_account


def _uses_mysql_upsert(session: Session) -> bool:
    return session.bind is not None and session.bind.dialect.name == "mysql"


@contextmanager
def _pid_registration_lock(session: Session, uma_pid: str) -> Iterator[None]:
    if not _uses_mysql_upsert(session):
        yield
        return

    # This narrows the empty-key race, but correctness does not depend on the
    # lock surviving the caller-owned commit. The current locking reads in
    # _raise_existing_registration_conflict and the unique constraint remain
    # authoritative after this service returns.
    lock_name = f"umacircle:game-account-pid:{uma_pid}"
    acquired = session.scalar(text("SELECT GET_LOCK(:lock_name, 10)"), {"lock_name": lock_name})
    if acquired != 1:
        raise IdentityStateError("could not acquire game account registration lock")
    try:
        yield
    finally:
        session.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})


def _raise_existing_registration_conflict(
    session: Session,
    *,
    discord_account_id: int,
    uma_pid: str,
    original_error: IntegrityError | None = None,
) -> None:
    # Locking reads are intentional: after a duplicate-key wait under MariaDB
    # REPEATABLE READ they must see the newly committed owner, not an older
    # consistent-read snapshot.
    existing_owned_account = session.scalar(
        select(GameAccount).where(GameAccount.discord_account_id == discord_account_id).with_for_update()
    )
    existing_pid_account = session.scalar(select(GameAccount).where(GameAccount.uma_pid == uma_pid).with_for_update())
    if existing_pid_account is not None:
        if existing_pid_account.discord_account_id == discord_account_id:
            raise AccountAlreadyRegisteredError("account is already registered")
        raise DuplicateUmaPidError("uma_pid is already registered")
    if existing_owned_account is not None:
        raise IdentityStateError("Discord account already has a registered game account")
    if original_error is not None:
        raise original_error


def _is_mariadb_duplicate_key_error(error: IntegrityError) -> bool:
    original = error.orig
    errno = getattr(original, "errno", None)
    if errno == 1062:
        return True
    arguments = getattr(original, "args", ())
    return bool(arguments) and arguments[0] == 1062


def _begin_sqlite_outer_transaction_if_needed(session: Session) -> None:
    if session.in_transaction() or session.bind is None or session.bind.dialect.name != "sqlite":
        return
    session.connection().exec_driver_sql("BEGIN")


def _validate_registration_name(value: str | None, *, field_name: str) -> None:
    if value is not None and len(value) > 100:
        raise IdentityStateError(f"{field_name} must not exceed 100 characters")


def _normalize_required_name(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise IdentityStateError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise IdentityStateError(f"{field_name} must not be empty")
    _validate_registration_name(normalized, field_name=field_name)
    return normalized


def resolve_owned_game_account(
    session: Session,
    *,
    discord_user_id: str,
    game_account_id: int | None = None,
) -> GameAccountDTO:
    """Resolve an explicitly selected or otherwise unambiguous Persona peer account."""

    discord_account = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id))
    if discord_account is None:
        raise AccountNotFoundError("discord account not found")
    if game_account_id is not None:
        selected_id = _normalize_positive_id(game_account_id)
        if discord_account.persona_id is None:
            raise AccountNotFoundError("Discord account is not linked to a Persona")
        game_account = session.scalar(
            select(GameAccount).where(
                GameAccount.id == selected_id,
                GameAccount.persona_id == discord_account.persona_id,
            )
        )
        if game_account is None:
            raise AccountNotFoundError("selected game account is not owned by the Discord account Persona")
        return _game_account_dto(game_account)
    return _game_account_dto(_resolve_owned_game_account_model(session, discord_account=discord_account))


def update_game_account_ingame_name(
    session: Session,
    *,
    game_account_id: int,
    ingame_name: str,
) -> GameAccountDTO:
    normalized_account_id = _normalize_positive_id(game_account_id)
    normalized_ingame_name = _normalize_required_name(ingame_name, field_name="ingame_name")
    with session.begin_nested():
        game_account = session.scalar(
            select(GameAccount).where(GameAccount.id == normalized_account_id).with_for_update()
        )
        if game_account is None:
            raise AccountNotFoundError("game account not found")
        if game_account.identity_status != IdentityStatus.CONFIRMED.value:
            raise IdentityStateError("only confirmed game accounts can change an ingame name")
        game_account.ingame_name = normalized_ingame_name
        session.flush()
    return _game_account_dto(game_account)


def refresh_discord_account_nickname(
    session: Session,
    *,
    discord_user_id: str,
    discord_nickname: str,
) -> bool:
    """Update the latest Discord display-name snapshot when that user uses the bot."""

    normalized_user_id = _normalize_discord_user_id(discord_user_id)
    normalized_nickname = _normalize_required_name(discord_nickname, field_name="discord_nickname")
    with session.begin_nested():
        discord_account = session.scalar(
            select(DiscordAccount).where(DiscordAccount.discord_user_id == normalized_user_id).with_for_update()
        )
        if discord_account is None or discord_account.discord_nickname == normalized_nickname:
            return False
        discord_account.discord_nickname = normalized_nickname
        session.flush()
    return True


def get_owned_account_info(session: Session, *, discord_user_id: str) -> AccountInfoDTO:
    """Return display-safe values when the actor has one unambiguous peer account."""

    discord_account = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id))
    if discord_account is None:
        raise AccountNotFoundError("discord account not found")
    game_account = _resolve_owned_game_account_model(session, discord_account=discord_account)
    row = session.execute(
        select(
            GameAccount.uma_pid,
            GameAccount.nickname,
            GameAccount.ingame_name,
            GameAccount.identity_status,
            CirclePointAccount.balance,
            GameAccount.created_at,
        )
        .join(CirclePointAccount, CirclePointAccount.persona_id == GameAccount.persona_id)
        .where(GameAccount.id == game_account.id)
    ).one_or_none()
    if row is None:
        raise AccountNotFoundError("registered game account or point account not found")
    return AccountInfoDTO(
        uma_pid=row.uma_pid,
        nickname=row.nickname,
        ingame_name=row.ingame_name,
        identity_status=row.identity_status,
        circle_point_balance=row.balance,
        created_at=database_datetime_as_utc(row.created_at),
    )


def _resolve_owned_game_account_model(session: Session, *, discord_account: DiscordAccount) -> GameAccount:
    if discord_account.persona_id is None:
        raise AccountNotFoundError("Discord account is not linked to a Persona")
    persona = session.get(Persona, discord_account.persona_id)
    if persona is None:
        raise IdentityStateError("Discord account Persona link is inconsistent")
    game_accounts = list(
        session.scalars(
            select(GameAccount).where(GameAccount.persona_id == persona.id).order_by(GameAccount.id).limit(2)
        )
    )
    if not game_accounts:
        raise AccountNotFoundError("Persona has no registered game account")
    if len(game_accounts) != 1:
        raise IdentityStateError("Persona has multiple game accounts; explicit game account selection is required")
    return game_accounts[0]


def confirm_game_account_identity(
    session: Session,
    *,
    game_account_id: int,
    uma_pid: str,
    confirmed_by_discord_user_id: str,
    resolution_note: str | None = None,
) -> GameAccountDTO:
    normalized_account_id = _normalize_positive_id(game_account_id)
    normalized_pid = normalize_uma_pid(uma_pid)
    actor_id = _normalize_discord_user_id(confirmed_by_discord_user_id)
    normalized_note = _normalize_optional_note(resolution_note)

    with session.begin_nested():
        game_account = session.scalar(
            select(GameAccount).where(GameAccount.id == normalized_account_id).with_for_update()
        )
        if game_account is None:
            raise AccountNotFoundError("game account not found")
        if game_account.identity_status == IdentityStatus.CONFIRMED.value:
            if game_account.uma_pid != normalized_pid:
                raise IdentityStateError("confirmed identity cannot be changed by backfill")
            return _game_account_dto(game_account)
        elif game_account.identity_status not in {
            IdentityStatus.PENDING.value,
            IdentityStatus.CONFLICT.value,
        }:
            raise IdentityStateError("game account has an unsupported identity state")

        duplicate = session.scalar(
            select(GameAccount)
            .where(GameAccount.uma_pid == normalized_pid, GameAccount.id != game_account.id)
            .with_for_update()
        )
        if duplicate is not None:
            raise DuplicateUmaPidError("uma_pid is already registered")

        game_account.uma_pid = normalized_pid
        game_account.identity_status = IdentityStatus.CONFIRMED.value
        task = session.scalar(
            select(IdentityBackfillTask)
            .where(IdentityBackfillTask.game_account_id == game_account.id)
            .with_for_update()
        )
        if task is not None:
            task.status = "resolved"
            task.resolved_by_discord_user_id = actor_id
            task.resolved_at = datetime.now(UTC)
            task.resolution_note = normalized_note
            task.conflict_detail_json = None
        session.flush()

    return _game_account_dto(game_account)


def _game_account_dto(game_account: GameAccount) -> GameAccountDTO:
    return GameAccountDTO(
        id=game_account.id,
        persona_id=game_account.persona_id,
        discord_account_id=game_account.discord_account_id,
        uma_pid=game_account.uma_pid,
        nickname=game_account.nickname,
        ingame_name=game_account.ingame_name,
        identity_status=game_account.identity_status,
    )


def _registration_dto(
    game_account: GameAccount,
    point_account: CirclePointAccount,
    *,
    initial_grant_transaction: CirclePointTransaction | None,
    initial_grant_amount: int,
    was_created: bool,
) -> GameAccountRegistrationDTO:
    return GameAccountRegistrationDTO(
        id=game_account.id,
        discord_account_id=game_account.discord_account_id,
        uma_pid=game_account.uma_pid,
        nickname=game_account.nickname,
        ingame_name=game_account.ingame_name,
        identity_status=game_account.identity_status,
        circle_point_account_id=point_account.id,
        circle_point_balance=point_account.balance,
        initial_grant_transaction_id=(initial_grant_transaction.id if initial_grant_transaction is not None else None),
        initial_grant_amount=initial_grant_amount,
        was_created=was_created,
    )


def _normalize_positive_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise IdentityStateError("game account ID must be a positive integer")
    return value


def _normalize_discord_user_id(value: str) -> str:
    if not isinstance(value, str):
        raise IdentityStateError("Discord user ID must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 32 or not normalized.isascii() or not normalized.isdigit():
        raise IdentityStateError("Discord user ID must contain 1 to 32 ASCII digits")
    return normalized


def _normalize_optional_note(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdentityStateError("resolution note must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 255 or any(ord(character) < 32 for character in normalized):
        raise IdentityStateError("resolution note contains unsupported text")
    return normalized
