from __future__ import annotations

from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.dtos import (
    PlayerLinkApprovalDTO,
    PlayerLinkCandidateDTO,
    PlayerLinkMutationDTO,
    PlayerLinkRequestDTO,
)
from umacircle_bot.services.player_link_requests import (
    ApprovePlayerLinkRequestCommand,
    CancelPlayerLinkRequestCommand,
    RejectPlayerLinkRequestCommand,
    ReviewPlayerLinkRequestCommand,
    RevisePlayerLinkRequestCommand,
    SubmitPlayerLinkRequestCommand,
    approve_player_link_request,
    cancel_player_link_request,
    get_active_player_link_request,
    get_owned_player_link_request,
    get_player_link_request_for_staff,
    list_player_link_requests,
    mark_player_link_request_for_review,
    reject_player_link_request,
    revise_player_link_request,
    search_player_link_candidates,
    search_player_link_candidates_for_terms,
    submit_player_link_request,
)


def execute_player_link_submit(
    command: SubmitPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: submit_player_link_request(
            session,
            command=command,
        )
    )


def execute_player_link_cancel(
    command: CancelPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: cancel_player_link_request(
            session,
            command=command,
        )
    )


def execute_player_link_revise(
    command: RevisePlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: revise_player_link_request(
            session,
            command=command,
        )
    )


def execute_player_link_approve(
    command: ApprovePlayerLinkRequestCommand,
) -> PlayerLinkApprovalDTO | PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: approve_player_link_request(
            session,
            command=command,
        )
    )


def execute_player_link_review(
    command: ReviewPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: mark_player_link_request_for_review(
            session,
            command=command,
        )
    )


def execute_player_link_reject(
    command: RejectPlayerLinkRequestCommand,
) -> PlayerLinkMutationDTO:
    return run_application_command(
        lambda session: reject_player_link_request(
            session,
            command=command,
        )
    )


def query_owned_player_link_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return run_application_query(
        lambda session: get_owned_player_link_request(
            session,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
        )
    )


def query_active_player_link_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return run_application_query(
        lambda session: get_active_player_link_request(
            session,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
        )
    )


def query_staff_player_link_queue(
    *,
    guild_id: str,
) -> tuple[PlayerLinkRequestDTO, ...]:
    return run_application_query(
        lambda session: list_player_link_requests(
            session,
            guild_id=guild_id,
            limit=25,
        )
    )


def query_staff_player_link_request(
    *,
    guild_id: str,
    request_id: int,
) -> PlayerLinkRequestDTO:
    return run_application_query(
        lambda session: get_player_link_request_for_staff(
            session,
            guild_id=guild_id,
            request_id=request_id,
        )
    )


def query_player_link_candidates(
    *,
    request_id: int,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    return run_application_query(
        lambda session: search_player_link_candidates(
            session,
            request_id=request_id,
            limit=10,
        )
    )


def query_player_link_candidates_for_terms(
    *,
    submitted_ingame_name: str,
    discord_nickname_snapshot: str,
    submitted_nickname_chunk: str | None,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    return run_application_query(
        lambda session: search_player_link_candidates_for_terms(
            session,
            submitted_ingame_name=submitted_ingame_name,
            discord_nickname_snapshot=discord_nickname_snapshot,
            submitted_nickname_chunk=submitted_nickname_chunk,
            limit=10,
        )
    )
