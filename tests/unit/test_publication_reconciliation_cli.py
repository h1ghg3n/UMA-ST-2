"""Tests for bounded publication-state reconciliation CLI."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uma_st2 import publication_reconciliation_entrypoint
from uma_st2.adapters.cli import (
    PUBLICATION_AWAITING_PROMOTION_DEFAULT_COUNT,
    PUBLICATION_UNKNOWN_LIST_DEFAULT_COUNT,
    PublicationAwaitingPromotionCliRequest,
    PublicationReconciliationCliAdapter,
    PublicationReconciliationCliError,
    PublicationUnknownListCliRequest,
    PublicationUnknownReconciliationCliRequest,
    parse_publication_reconciliation_cli_request,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    PUBLICATION_AWAITING_PROMOTION_MAX_COUNT,
    PUBLICATION_UNKNOWN_LIST_MAX_COUNT,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_ROUND_RESULT_EVENT_TYPE,
    PromotedAwaitingPublications,
    PublicationFailureStage,
    PublicationUnknownResolution,
    ReconciledUnknownPublication,
    ReconcileUnknownPublication,
    UnknownPublicationDelivery,
)
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _receipt() -> PromotedAwaitingPublications:
    return PromotedAwaitingPublications(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        target_channel_id="123456789",
        promoted_count=3,
        promoted_at=NOW,
    )


def _unknown() -> UnknownPublicationDelivery:
    return UnknownPublicationDelivery(
        publication_id=51,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
        event_key="win5-round:81:result",
        source_kind="win5_round",
        source_id=81,
        target_channel_id="123456789",
        payload_fingerprint="b" * 64,
        attempt_count=2,
        discord_message_id=None,
        error_code="discord_send_timeout",
        failure_stage=PublicationFailureStage.SEND,
        created_at=NOW,
        updated_at=NOW,
    )


def _reconciled() -> ReconciledUnknownPublication:
    return ReconciledUnknownPublication(
        publication_id=51,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        attempt_count=2,
        resolution=PublicationUnknownResolution.CONFIRMED_SENT,
        status=PublicationStatus.SENT,
        discord_message_id="555555555",
        reconciled_at=NOW,
    )


def test_parser_requires_action_guild_and_supported_destination() -> None:
    assert parse_publication_reconciliation_cli_request(
        [
            "promote-awaiting",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        ]
    ) == PublicationAwaitingPromotionCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        limit=PUBLICATION_AWAITING_PROMOTION_DEFAULT_COUNT,
    )
    assert (
        parse_publication_reconciliation_cli_request(
            [
                "promote-awaiting",
                "--guild-id",
                "987654321",
                "--destination-kind",
                MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                "--limit",
                "7",
            ]
        ).limit
        == 7
    )


def test_parser_builds_unknown_list_and_resolution_requests() -> None:
    assert parse_publication_reconciliation_cli_request(
        [
            "list-unknown",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        ]
    ) == PublicationUnknownListCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        limit=PUBLICATION_UNKNOWN_LIST_DEFAULT_COUNT,
    )
    assert parse_publication_reconciliation_cli_request(
        [
            "mark-unknown-sent",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--publication-id",
            "51",
            "--attempt-count",
            "2",
            "--discord-message-id",
            "555555555",
        ]
    ) == PublicationUnknownReconciliationCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        publication_id=51,
        attempt_count=2,
        resolution=PublicationUnknownResolution.CONFIRMED_SENT,
        discord_message_id="555555555",
    )
    assert parse_publication_reconciliation_cli_request(
        [
            "retry-unknown-zero-send",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--publication-id",
            "51",
            "--attempt-count",
            "2",
        ]
    ) == PublicationUnknownReconciliationCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        publication_id=51,
        attempt_count=2,
        resolution=PublicationUnknownResolution.RETRY_ZERO_SEND,
    )


@pytest.mark.parametrize(
    "argv",
    (
        [],
        ["promote-awaiting", "--guild-id", "0", "--destination-kind", WIN5_ANNOUNCEMENT_DESTINATION_KIND],
        ["promote-awaiting", "--guild-id", "987654321", "--destination-kind", "log"],
        [
            "promote-awaiting",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--limit",
            "0",
        ],
        [
            "promote-awaiting",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--limit",
            str(PUBLICATION_AWAITING_PROMOTION_MAX_COUNT + 1),
        ],
        [
            "list-unknown",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--limit",
            str(PUBLICATION_UNKNOWN_LIST_MAX_COUNT + 1),
        ],
        [
            "mark-unknown-sent",
            "--guild-id",
            "987654321",
            "--destination-kind",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "--publication-id",
            "0",
            "--attempt-count",
            "2",
            "--discord-message-id",
            "555555555",
        ],
    ),
)
def test_parser_rejects_unbounded_or_malformed_requests(argv: list[str]) -> None:
    with pytest.raises((SystemExit, PublicationReconciliationCliError)):
        parse_publication_reconciliation_cli_request(argv)


class FakeCommands:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, str, int]] = []
        self.reconciliation_calls: list[ReconcileUnknownPublication] = []

    def promote_awaiting(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> PromotedAwaitingPublications:
        self.calls.append((guild_id, destination_kind, max_count))
        if self.failure is not None:
            raise self.failure
        return _receipt()

    def reconcile_unknown(
        self,
        command: ReconcileUnknownPublication,
    ) -> ReconciledUnknownPublication:
        self.reconciliation_calls.append(command)
        if self.failure is not None:
            raise self.failure
        return _reconciled()


class FakeQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def list_unknown(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> tuple[UnknownPublicationDelivery, ...]:
        self.calls.append((guild_id, destination_kind, max_count))
        return (_unknown(),)


def test_cli_adapter_forwards_only_bounded_identity_to_application() -> None:
    commands = FakeCommands()
    queries = FakeQueries()
    adapter = PublicationReconciliationCliAdapter(  # type: ignore[arg-type]
        commands=commands,
        queries=queries,
    )
    request = PublicationAwaitingPromotionCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        limit=7,
    )

    assert adapter.execute(request) == _receipt()
    assert commands.calls == [("987654321", WIN5_ANNOUNCEMENT_DESTINATION_KIND, 7)]


def test_cli_adapter_routes_unknown_query_and_reconciliation() -> None:
    commands = FakeCommands()
    queries = FakeQueries()
    adapter = PublicationReconciliationCliAdapter(  # type: ignore[arg-type]
        commands=commands,
        queries=queries,
    )

    assert adapter.execute(
        PublicationUnknownListCliRequest(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            limit=7,
        )
    ) == (_unknown(),)
    assert queries.calls == [("987654321", WIN5_ANNOUNCEMENT_DESTINATION_KIND, 7)]

    request = PublicationUnknownReconciliationCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        publication_id=51,
        attempt_count=2,
        resolution=PublicationUnknownResolution.CONFIRMED_SENT,
        discord_message_id="555555555",
    )
    assert adapter.execute(request) == _reconciled()
    assert commands.reconciliation_calls == [
        ReconcileUnknownPublication(
            publication_id=51,
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            expected_attempt_count=2,
            resolution=PublicationUnknownResolution.CONFIRMED_SENT,
            discord_message_id="555555555",
        )
    ]


class FakeSettings:
    database_url_value = "mysql+pymysql://user:password@db/app?charset=utf8mb4"


class FakeDatabaseRuntime:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


@pytest.mark.parametrize("failure", [None, RuntimeError("promotion failed")])
def test_entrypoint_always_disposes_database_runtime(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | None,
) -> None:
    database_runtime = FakeDatabaseRuntime()
    commands = FakeCommands(failure=failure)
    queries = FakeQueries()
    monkeypatch.setattr(
        publication_reconciliation_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *_args, **_kwargs: database_runtime),
    )
    monkeypatch.setattr(
        publication_reconciliation_entrypoint,
        "compose_publication_delivery",
        lambda _runtime: commands,
    )
    monkeypatch.setattr(
        publication_reconciliation_entrypoint,
        "compose_publication_delivery_queries",
        lambda _runtime: queries,
    )
    request = PublicationAwaitingPromotionCliRequest(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        limit=7,
    )

    if failure is None:
        assert (
            publication_reconciliation_entrypoint.run_publication_reconciliation(
                FakeSettings(),  # type: ignore[arg-type]
                request,
            )
            == _receipt()
        )
    else:
        with pytest.raises(RuntimeError, match="promotion failed"):
            publication_reconciliation_entrypoint.run_publication_reconciliation(
                FakeSettings(),  # type: ignore[arg-type]
                request,
            )

    assert database_runtime.dispose_calls == 1


def test_success_output_lists_only_bounded_unknown_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    publication_reconciliation_entrypoint._print_success((_unknown(),))

    output = capsys.readouterr().out
    assert "Unknown publications: 1" in output
    assert "publication_id=51" in output
    assert "attempt_count=2" in output
    assert "fingerprint=" + "b" * 64 in output
    assert "payload_json" not in output


def test_main_reports_invalid_snowflake_without_opening_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_settings() -> object:
        raise AssertionError("database settings must not load")

    monkeypatch.setattr(
        publication_reconciliation_entrypoint,
        "DatabaseSettings",
        unexpected_settings,
    )

    with pytest.raises(SystemExit) as raised:
        publication_reconciliation_entrypoint.main(
            [
                "promote-awaiting",
                "--guild-id",
                "0",
                "--destination-kind",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            ]
        )

    assert raised.value.code == 1
    assert "guild_id must be a positive decimal Discord snowflake" in capsys.readouterr().err
