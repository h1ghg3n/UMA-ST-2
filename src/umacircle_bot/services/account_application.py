from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import GameAccount
from umacircle_bot.domain.errors import AccountNotFoundError
from umacircle_bot.services.accounts import (
    get_owned_account_info,
    refresh_discord_account_nickname,
    update_game_account_ingame_name,
)
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    autocomplete_registered_accounts,
)
from umacircle_bot.services.circle_points import adjust_circle_points, grant_circle_points
from umacircle_bot.services.dtos import (
    AccountOverviewDTO,
    AccountRegistrationMutationDTO,
    AccountRegistrationRequestDTO,
    CirclePointMutationDTO,
    GameAccountDTO,
)
from umacircle_bot.services.registration_requests import (
    ApproveAccountRegistrationRequestCommand,
    CancelAccountRegistrationRequestCommand,
    RejectAccountRegistrationRequestCommand,
    SubmitAccountRegistrationRequestCommand,
    approve_account_registration_request,
    cancel_account_registration_request,
    get_owned_account_registration_request,
    reject_account_registration_request,
    submit_account_registration_request,
)
from umacircle_bot.services.win5 import get_account_win5_info


@dataclass(frozen=True, slots=True)
class StaffAccountPointCommand:
    game_account_id: int
    amount: int
    reason: str
    actor_discord_user_id: str
    idempotency_key: str


def execute_discord_nickname_refresh(
    *,
    discord_user_id: str,
    discord_nickname: str,
) -> bool:
    """Refresh one Discord display-name snapshot in an application-owned transaction."""

    return run_application_command(
        lambda session: refresh_discord_account_nickname(
            session,
            discord_user_id=discord_user_id,
            discord_nickname=discord_nickname,
        )
    )


def query_account_overview(*, discord_user_id: str) -> AccountOverviewDTO:
    """Return the member-facing account and WIN5 summary through one query boundary."""

    return run_application_query(
        lambda session: AccountOverviewDTO(
            account=get_owned_account_info(
                session,
                discord_user_id=discord_user_id,
            ),
            win5=get_account_win5_info(
                session,
                discord_user_id=discord_user_id,
            ),
        )
    )


def query_staff_registered_accounts(
    *,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    """Return registered account choices through the read-only application boundary."""

    return run_application_query(
        lambda session: autocomplete_registered_accounts(
            session,
            query=query,
        )
    )


def query_owned_account_registration_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> AccountRegistrationRequestDTO | None:
    return run_application_query(
        lambda session: get_owned_account_registration_request(
            session,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
        )
    )


def execute_account_registration_submit(
    command: SubmitAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    return run_application_command(
        lambda session: submit_account_registration_request(
            session,
            command=command,
        )
    )


def execute_account_registration_cancel(
    command: CancelAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    return run_application_command(
        lambda session: cancel_account_registration_request(
            session,
            command=command,
        )
    )


def execute_account_registration_approve(
    command: ApproveAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    return run_application_command(
        lambda session: approve_account_registration_request(
            session,
            command=command,
        )
    )


def execute_account_registration_reject(
    command: RejectAccountRegistrationRequestCommand,
) -> AccountRegistrationMutationDTO:
    return run_application_command(
        lambda session: reject_account_registration_request(
            session,
            command=command,
        )
    )


def execute_staff_ingame_name_update(
    *,
    game_account_id: int,
    ingame_name: str,
) -> GameAccountDTO:
    return run_application_command(
        lambda session: update_game_account_ingame_name(
            session,
            game_account_id=game_account_id,
            ingame_name=ingame_name,
        )
    )


def execute_staff_room_point_grant(
    command: StaffAccountPointCommand,
) -> CirclePointMutationDTO:
    def operation(session: Session) -> CirclePointMutationDTO:
        uma_pid = _lock_registered_uma_pid(
            session,
            game_account_id=command.game_account_id,
        )
        transaction = grant_circle_points(
            session,
            uma_pid=uma_pid,
            amount=command.amount,
            reason=command.reason,
            granted_by_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
        )
        return CirclePointMutationDTO(transaction=transaction, uma_pid=uma_pid)

    return run_application_command(operation)


def execute_staff_room_point_adjustment(
    command: StaffAccountPointCommand,
) -> CirclePointMutationDTO:
    def operation(session: Session) -> CirclePointMutationDTO:
        uma_pid = _lock_registered_uma_pid(
            session,
            game_account_id=command.game_account_id,
        )
        transaction = adjust_circle_points(
            session,
            uma_pid=uma_pid,
            amount=command.amount,
            reason=command.reason,
            adjusted_by_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
        )
        return CirclePointMutationDTO(transaction=transaction, uma_pid=uma_pid)

    return run_application_command(operation)


def _lock_registered_uma_pid(session: Session, *, game_account_id: int) -> str:
    uma_pid = session.scalar(
        select(GameAccount.uma_pid)
        .where(
            GameAccount.id == game_account_id,
            GameAccount.uma_pid.is_not(None),
        )
        .with_for_update()
    )
    if uma_pid is None:
        raise AccountNotFoundError("game account not found")
    return uma_pid
