"""Application tests for private Account/Persona status queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.identity import (
    AccountEligibilityState,
    AccountIdentityDetails,
    AccountMatchHistoryPage,
    AccountMatchSummary,
    AccountRegistrationRequestSummary,
    AccountStatusOverview,
    AccountStatusQueries,
    AccountWin5HistoryPage,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)


def _match_summary() -> AccountMatchSummary:
    return AccountMatchSummary(
        season_key="2026-split-2",
        season_name="2026 Split 2",
        participated_match_count=0,
        entry_count=0,
        win_count=0,
        top3_count=0,
        average_rank=None,
        excluded_terminal_match_count=0,
    )


def _request() -> AccountRegistrationRequestSummary:
    return AccountRegistrationRequestSummary(
        request_id=1,
        game_region=GameRegion.KR,
        pid_hint="••••1234",
        nickname="신청 계정",
        status=RegistrationRequestStatus.PENDING,
        reason=None,
        created_at=NOW,
        resolved_at=None,
    )


def _overview(
    *,
    status: PersonaStatus | None = PersonaStatus.NORMAL,
    wallet_balance: int | None = 500,
    eligible_account_count: int = 1,
    request: AccountRegistrationRequestSummary | None = None,
) -> AccountStatusOverview:
    linked = status is not None
    return AccountStatusOverview(
        persona_id="persona-1" if linked else None,
        display_name="테스트 Persona" if linked else None,
        persona_status=status,
        wallet_balance=wallet_balance if linked else None,
        game_account_count=1 if linked else 0,
        eligible_game_account_count=eligible_account_count if linked else 0,
        registration_request=request,
        match=_match_summary(),
        win5=None,
    )


class RecordingRepository:
    def __init__(self, overview: AccountStatusOverview | None = None) -> None:
        self.overview = overview or _overview()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_overview(self, **kwargs: object) -> AccountStatusOverview:
        self.calls.append(("overview", kwargs))
        return self.overview

    def get_match_history(self, **kwargs: object) -> AccountMatchHistoryPage:
        self.calls.append(("match", kwargs))
        return AccountMatchHistoryPage(
            season_key=str(kwargs["match_season_key"]),
            season_name=str(kwargs["match_season_name"]),
            page=int(kwargs["page"]),
            total_count=0,
            items=(),
        )

    def get_win5_history(self, **kwargs: object) -> AccountWin5HistoryPage:
        self.calls.append(("win5", kwargs))
        return AccountWin5HistoryPage(season=None, page=int(kwargs["page"]), total_count=0, items=())

    def get_identity_details(self, **kwargs: object) -> AccountIdentityDetails:
        self.calls.append(("identity", kwargs))
        return AccountIdentityDetails(
            persona_id="persona-1",
            display_name="테스트 Persona",
            persona_status=PersonaStatus.NORMAL,
            page=int(kwargs["page"]),
            total_count=0,
            accounts=(),
            registration_request=None,
        )


@dataclass
class RecordingUnitOfWork:
    account_status_queries: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _queries(repository: RecordingRepository) -> tuple[AccountStatusQueries, RecordingFactory]:
    factory = RecordingFactory(repository)
    return AccountStatusQueries(QueryRunner(factory), clock=lambda: NOW), factory


def test_overview_uses_current_kst_half_and_read_only_uow() -> None:
    repository = RecordingRepository()
    queries, factory = _queries(repository)

    result = queries.get_overview(discord_user_id="123", guild_id="987")

    assert result.eligibility is AccountEligibilityState.ELIGIBLE
    name, call = repository.calls[0]
    assert name == "overview"
    assert call["discord_user_id"] == "123"
    assert call["guild_id"] == "987"
    assert call["match_season_key"] == "2026-split-2"
    assert call["match_starts_at"] == datetime(2026, 6, 30, 15, 0, tzinfo=UTC)
    assert call["match_ends_at"] == datetime(2026, 12, 31, 15, 0, tzinfo=UTC)
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    ("overview", "expected"),
    (
        (_overview(status=PersonaStatus.WARNING), AccountEligibilityState.ELIGIBLE),
        (_overview(status=PersonaStatus.PENDING_APPROVAL), AccountEligibilityState.APPROVAL_PENDING),
        (_overview(status=PersonaStatus.EXPELLED), AccountEligibilityState.RESTRICTED),
        (_overview(wallet_balance=None), AccountEligibilityState.WALLET_REQUIRED),
        (_overview(eligible_account_count=0), AccountEligibilityState.GAME_ACCOUNT_REQUIRED),
        (_overview(status=None, request=_request()), AccountEligibilityState.REGISTRATION_PENDING),
        (_overview(status=None), AccountEligibilityState.UNREGISTERED),
    ),
)
def test_overview_derives_explicit_eligibility_reason(
    overview: AccountStatusOverview,
    expected: AccountEligibilityState,
) -> None:
    assert overview.eligibility is expected


def test_each_tab_uses_a_fresh_query_uow_and_bounded_page_offset() -> None:
    repository = RecordingRepository()
    queries, factory = _queries(repository)

    queries.get_match_history(discord_user_id="123", page=2)
    queries.get_win5_history(discord_user_id="123", page=1)
    queries.get_identity_details(discord_user_id="123", guild_id="987", page=3)

    assert [name for name, _ in repository.calls] == ["match", "win5", "identity"]
    assert repository.calls[0][1]["offset"] == 10
    assert repository.calls[1][1]["offset"] == 5
    assert repository.calls[2][1]["offset"] == 15
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)
    assert len(factory.created) == 3


@pytest.mark.parametrize(("discord_user_id", "guild_id"), (("", "987"), ("123", ""), ("x" * 33, "987")))
def test_invalid_actor_context_fails_before_opening_uow(discord_user_id: str, guild_id: str) -> None:
    queries, factory = _queries(RecordingRepository())

    with pytest.raises(ValueError):
        queries.get_overview(discord_user_id=discord_user_id, guild_id=guild_id)

    assert factory.created == []


def test_negative_page_fails_before_opening_uow() -> None:
    queries, factory = _queries(RecordingRepository())

    with pytest.raises(ValueError):
        queries.get_match_history(discord_user_id="123", page=-1)

    assert factory.created == []
