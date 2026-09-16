from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from uma_st2.domain.identity import (
    DiscordAccount,
    DiscordAccountInvariantError,
    GameAccount,
    GameAccountInvariantError,
    GameAccountRegistrationRequest,
    GameRegion,
    Persona,
    PersonaInvariantError,
    PersonaStatus,
    RegistrationRequestInvariantError,
    RegistrationRequestStatus,
    normalize_region,
)


def _make_datetime() -> datetime:
    return datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def test_canonical_persona_status_vocab_is_exact() -> None:
    assert {status.value for status in PersonaStatus} == {
        "normal",
        "warning",
        "pending_approval",
        "expelled",
        "withdrawn",
    }


def test_canonical_game_region_vocab_is_exact() -> None:
    assert {region.value for region in GameRegion} == {"KR", "JP"}


def test_normalize_region_allows_canonical_input_and_disallows_invalid() -> None:
    assert normalize_region("KR") == GameRegion.KR

    with pytest.raises(ValueError):
        normalize_region("kr")


def test_canonical_registration_request_status_vocab_is_exact() -> None:
    assert {status.value for status in RegistrationRequestStatus} == {
        "pending",
        "approved",
        "cancelled",
    }


def test_strings_are_not_trimmed_in_identity_entities() -> None:
    persona = Persona(id=" persona-0 ", display_name=" alpha ", status=PersonaStatus.WARNING)

    assert persona.id == " persona-0 "
    assert persona.display_name == " alpha "


def test_discord_account_string_fields_are_not_trimmed() -> None:
    account = DiscordAccount(id=10, discord_user_id=" 1001 ")

    assert account.discord_user_id == " 1001 "
    assert account.id == 10


def test_game_account_string_fields_are_not_trimmed() -> None:
    account = GameAccount(
        id=11,
        persona_id=" persona-0 ",
        game_region=GameRegion.KR,
        uma_pid=" 12345 ",
        nickname=" runner ",
        affiliation=" team ",
    )

    assert account.persona_id == " persona-0 "
    assert account.uma_pid == " 12345 "
    assert account.nickname == " runner "
    assert account.affiliation == " team "


def test_game_account_registration_request_fields_are_not_trimmed() -> None:
    request = GameAccountRegistrationRequest(
        id=100,
        guild_id=" guild-1 ",
        requester_discord_user_id=" 1001 ",
        discord_display_name_snapshot=" display ",
        game_region="KR",
        uma_pid=" 123456 ",
        nickname=" runner ",
        created_at=_make_datetime(),
    )

    assert request.guild_id == " guild-1 "
    assert request.requester_discord_user_id == " 1001 "
    assert request.discord_display_name_snapshot == " display "
    assert request.uma_pid == " 123456 "
    assert request.nickname == " runner "


def test_discord_account_can_be_temporarily_unlinked_and_relinked() -> None:
    persona = Persona(id="persona-1", display_name="alpha")
    account = DiscordAccount(id=12, discord_user_id="1001")

    assert account.persona_id is None
    assert not account.is_linked

    linked = account.link_to_persona(persona.id)
    assert linked.persona_id == persona.id
    assert linked.is_linked

    unlinked = linked.unlink()
    assert unlinked.persona_id is None
    assert not unlinked.is_linked


def test_persona_may_own_multiple_discord_accounts() -> None:
    persona = Persona(id="persona-2", display_name="alpha")
    first = DiscordAccount(id=11, discord_user_id="1002", persona_id=persona.id)
    second = DiscordAccount(id=13, discord_user_id="1003", persona_id=persona.id)

    assert first.persona_id == persona.id
    assert second.persona_id == persona.id


def test_persona_may_own_multiple_game_accounts() -> None:
    persona = Persona(id="persona-2a", display_name="alpha")
    first = GameAccount(
        id=11,
        persona_id=persona.id,
        game_region=GameRegion.KR,
        uma_pid="123",
        nickname="runner1",
    )
    second = GameAccount(
        id=12,
        persona_id=persona.id,
        game_region="JP",
        uma_pid="456",
        nickname="runner2",
    )

    assert first.persona_id == persona.id
    assert second.persona_id == persona.id


def test_game_account_must_always_belong_to_persona() -> None:
    with pytest.raises(GameAccountInvariantError):
        GameAccount(
            id=1,
            persona_id=123,
            game_region=GameRegion.KR,
            uma_pid="12345",
            nickname="runner",
        )


def test_game_account_identity_key_uses_game_region_and_pid() -> None:
    account = GameAccount(
        id=2,
        persona_id="persona-3",
        game_region="KR",
        uma_pid="123456",
        nickname="runner",
    )

    assert account.identity_key == (GameRegion.KR, "123456")


def test_historical_game_account_may_have_unknown_pid() -> None:
    account = GameAccount(
        id=20,
        persona_id="persona-3",
        game_region="KR",
        uma_pid=None,
        nickname="historical runner",
    )

    assert account.uma_pid is None
    assert account.identity_key is None


def test_game_region_is_immutable() -> None:
    account = GameAccount(
        id=3,
        persona_id="persona-3",
        game_region="KR",
        uma_pid="123456",
        nickname="runner",
    )

    with pytest.raises(FrozenInstanceError):
        account.game_region = GameRegion.JP


def test_game_account_does_not_carry_main_or_sub_semantics() -> None:
    account = GameAccount(
        id=4,
        persona_id="persona-3",
        game_region=GameRegion.KR,
        uma_pid="9999",
        nickname="runner",
        affiliation="team",
    )

    assert not hasattr(account, "main_game_account_id")
    assert not hasattr(account, "main_account")
    assert not hasattr(account, "sub_account")


def test_affiliation_is_nullable_in_game_account() -> None:
    account = GameAccount(
        id=5,
        persona_id="persona-4",
        game_region="KR",
        uma_pid="777",
        nickname="runner",
        affiliation=None,
    )

    assert account.affiliation is None


def test_affiliation_whitespace_is_preserved() -> None:
    account = GameAccount(
        id=22,
        persona_id="persona-4",
        game_region="KR",
        uma_pid="778",
        nickname="runner",
        affiliation="   ",
    )

    assert account.affiliation == "   "


def test_game_account_has_no_legacy_identity_fields() -> None:
    account = GameAccount(
        id=6,
        persona_id="persona-5",
        game_region="KR",
        uma_pid="111",
        nickname="runner",
    )

    assert not hasattr(account, "identity_status")
    assert not hasattr(account, "ingame_name")


def test_registration_request_starts_pending() -> None:
    request = GameAccountRegistrationRequest(
        id=100,
        guild_id="guild-1",
        requester_discord_user_id="1001",
        discord_display_name_snapshot="runner display",
        game_region="KR",
        uma_pid="123",
        nickname="runner",
        created_at=_make_datetime(),
    )

    assert request.status == RegistrationRequestStatus.PENDING
    assert request.is_pending
    assert request.is_approved is False
    assert request.is_cancelled is False
    assert request.resolved_at is None


def test_registration_request_status_can_be_normalized() -> None:
    request = GameAccountRegistrationRequest(
        id=110,
        guild_id="guild-1",
        requester_discord_user_id="1001",
        discord_display_name_snapshot="runner display",
        game_region="KR",
        uma_pid="123",
        nickname="runner",
        created_at=_make_datetime(),
        status="approved",
    )

    assert request.status == RegistrationRequestStatus.APPROVED


def test_registration_request_keeps_reason_and_resolved_fields() -> None:
    request = GameAccountRegistrationRequest(
        id=111,
        guild_id="guild-1",
        requester_discord_user_id="1002",
        discord_display_name_snapshot="runner display",
        game_region="KR",
        uma_pid="124",
        nickname="runner",
        created_at=_make_datetime(),
        status="cancelled",
        reason="manual review",
        resolved_at=_make_datetime(),
    )

    assert request.status == RegistrationRequestStatus.CANCELLED
    assert request.reason == "manual review"
    assert request.resolved_at is not None


def test_invalid_status_is_rejected_for_registration_request() -> None:
    with pytest.raises(RegistrationRequestInvariantError):
        GameAccountRegistrationRequest(
            id=109,
            guild_id="guild-1",
            requester_discord_user_id="1001",
            discord_display_name_snapshot="runner display",
            game_region="KR",
            uma_pid="123",
            nickname="runner",
            created_at=_make_datetime(),
            status="rejected",
        )


def test_invalid_persona_id_raises_domain_error() -> None:
    with pytest.raises(PersonaInvariantError):
        Persona(id="persona", display_name=123)


def test_discord_account_invalid_payload_raises_domain_error() -> None:
    with pytest.raises(DiscordAccountInvariantError):
        DiscordAccount(id=0, discord_user_id=1001)
