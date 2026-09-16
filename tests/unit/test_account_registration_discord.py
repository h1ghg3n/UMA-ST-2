"""Private Discord Account registration Modal tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    AccountInteractionContext,
    AccountRegistrationDiscordAdapter,
    AccountRegistrationModal,
)
from uma_st2.application.identity import (
    AccountRegistrationAlreadyPendingError,
    AccountRegistrationRequestSnapshot,
    SubmitAccountRegistrationRequest,
    SubmittedAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion, RegistrationRequestStatus

NOW = datetime(2026, 9, 2, 5, 30, tzinfo=UTC)


class RecordingCommands:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[SubmitAccountRegistrationRequest] = []

    def submit_registration_request(
        self,
        command: SubmitAccountRegistrationRequest,
    ) -> SubmittedAccountRegistrationRequest:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return SubmittedAccountRegistrationRequest(
            snapshot=AccountRegistrationRequestSnapshot(
                request_id=51,
                discord_account_id=7,
                guild_id=command.guild_id,
                requester_discord_user_id=command.actor_discord_user_id,
                discord_display_name_snapshot=command.discord_display_name_snapshot,
                game_region=command.game_region,
                uma_pid=command.uma_pid,
                nickname=command.nickname,
                affiliation=command.affiliation,
                status=RegistrationRequestStatus.PENDING,
                created_at=NOW,
            )
        )


class RecordingPreparation:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return True


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


class RecordingResponse:
    def __init__(self) -> None:
        self.modal: discord.ui.Modal | None = None
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modal = modal

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    def is_done(self) -> bool:
        return False


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id, display_name="Discord 표시명")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    *,
    error: Exception | None = None,
) -> tuple[
    AccountRegistrationDiscordAdapter,
    RecordingCommands,
    RecordingPreparation,
    RecordingAuthorization,
]:
    commands = RecordingCommands(error=error)
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        AccountRegistrationDiscordAdapter(
            commands=commands,  # type: ignore[arg-type]
            prepare_command=preparation,
            authorize_interaction=authorization,
            run_application=_inline,  # type: ignore[arg-type]
        ),
        commands,
        preparation,
        authorization,
    )


def test_register_opens_bound_modal_without_canonical_write() -> None:
    adapter, commands, preparation, authorization = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(adapter.open_registration(interaction, game_region="KR"))

    assert authorization.calls == [(interaction, "account.register")]
    assert preparation.calls == []
    assert commands.calls == []
    assert isinstance(interaction.response.modal, AccountRegistrationModal)
    modal = interaction.response.modal
    assert modal is not None
    assert len(modal.children) == 3


def test_modal_submit_reauthorizes_and_returns_private_masked_receipt() -> None:
    adapter, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=777)
    context = AccountInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.submit_request(
            interaction,
            context=context,
            game_region=GameRegion.JP,
            uma_pid=" 123456789 ",
            nickname=" 게임 닉네임 ",
            affiliation=" 소속 ",
        )
    )

    assert preparation.calls == [(interaction, "account.register", True)]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == command.correlation_id == "777"
    assert command.discord_display_name_snapshot == "Discord 표시명"
    assert command.uma_pid == "123456789"
    assert interaction.edits
    receipt = str(interaction.edits[0]["content"])
    assert "요청 ID: `51`" in receipt
    assert "••••6789" in receipt
    assert "123456789" not in receipt
    allowed_mentions = interaction.edits[0]["allowed_mentions"]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.to_dict() == {"parse": []}


def test_modal_submit_rejects_wrong_bound_context_before_command() -> None:
    adapter, commands, preparation, _ = _adapter()
    opener = RecordingInteraction()
    wrong_channel = RecordingInteraction(channel_id=999)

    asyncio.run(
        adapter.submit_request(
            wrong_channel,
            context=AccountInteractionContext.from_interaction(opener),
            game_region=GameRegion.KR,
            uma_pid="123",
            nickname="닉네임",
            affiliation=None,
        )
    )

    assert preparation.calls == []
    assert commands.calls == []
    assert wrong_channel.response.messages
    assert wrong_channel.response.messages[0][1]["ephemeral"] is True


def test_pending_request_error_is_a_private_actionable_receipt() -> None:
    adapter, commands, _, _ = _adapter(error=AccountRegistrationAlreadyPendingError(88))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.submit_request(
            interaction,
            context=AccountInteractionContext.from_interaction(interaction),
            game_region=GameRegion.KR,
            uma_pid="123",
            nickname="닉네임",
            affiliation=None,
        )
    )

    assert len(commands.calls) == 1
    assert "요청 ID: `88`" in str(interaction.edits[0]["content"])
    assert "/account status" in str(interaction.edits[0]["content"])


def test_invalid_modal_input_is_private_and_never_calls_application_command() -> None:
    adapter, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.submit_request(
            interaction,
            context=AccountInteractionContext.from_interaction(interaction),
            game_region=GameRegion.KR,
            uma_pid="0123",
            nickname="닉네임",
            affiliation=None,
        )
    )

    assert preparation.calls == [(interaction, "account.register", True)]
    assert commands.calls == []
    assert "0으로 시작하지 않는 숫자" in str(interaction.edits[0]["content"])
