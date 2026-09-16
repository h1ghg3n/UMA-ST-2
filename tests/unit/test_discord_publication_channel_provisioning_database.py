"""SQLite persistence tests for automatic publication-channel binding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
    DiscordPublicationChannelProvisioningInvalidSourceError,
    ProvisionDiscordPublicationChannel,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_ROUND_RESULT_EVENT_TYPE,
)
from uma_st2.compose import (
    compose_discord_publication_channel_provisioning_commands,
    compose_discord_publication_channel_provisioning_queries,
)
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    Base,
    BotGuildSettingORM,
    DiscordPublicationORM,
    OperationORM,
    SettingsOperationORM,
)

NOW = datetime(2026, 9, 5, 5, 30, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
GUILD_ID = "987"
CHANNEL_ID = "654"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime, *, enabled: bool = True, channel_id: str | None = None) -> None:
    with runtime.session_factory.begin() as session:
        session.add(
            BotGuildSettingORM(
                guild_id=GUILD_ID,
                win5_announcement_channel_id=None,
                match_announcement_channel_id=channel_id,
                log_channel_id="203",
                operator_role_id="301",
                bot_manager_role_id="302",
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=enabled,
                match_odds_refresh_mode="live",
                match_odds_refresh_next_at=STORED_NOW + timedelta(minutes=1),
                match_odds_refresh_sequence=17,
                match_odds_last_projection_fingerprint="a" * 64,
                created_at=STORED_NOW - timedelta(days=10),
                updated_at=STORED_NOW,
            )
        )
        for publication_id in (71, 72):
            session.add(
                DiscordPublicationORM(
                    id=publication_id,
                    guild_id=GUILD_ID,
                    destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                    event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
                    event_key=f"match-open:{publication_id}",
                    source_kind="match",
                    source_id=publication_id,
                    target_channel_id=None,
                    payload_json={"schema_version": 1},
                    payload_fingerprint=chr(26 + publication_id) * 64,
                    status="awaiting_channel",
                    attempt_count=0,
                    created_at=STORED_NOW + timedelta(seconds=publication_id),
                    updated_at=STORED_NOW + timedelta(seconds=publication_id),
                )
            )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _command(runtime: DatabaseRuntime, *, key: str) -> ProvisionDiscordPublicationChannel:
    target = compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=GUILD_ID)
    assert target is not None
    return ProvisionDiscordPublicationChannel(
        target=target,
        target_channel_id=CHANNEL_ID,
        actor_discord_user_id="900",
        idempotency_key=key,
        correlation_id=key,
    )


def test_bind_commits_settings_audit_and_only_triggering_publication() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="automatic-channel-71")

        created = compose_discord_publication_channel_provisioning_commands(runtime).provision(command)
        retry = compose_discord_publication_channel_provisioning_commands(runtime).provision(command)

        assert retry.operation_id == created.operation_id
        assert retry.exact_retry is True
        with runtime.session_factory() as session:
            setting = session.get(BotGuildSettingORM, GUILD_ID)
            first = session.get(DiscordPublicationORM, 71)
            second = session.get(DiscordPublicationORM, 72)
            operation = session.get(OperationORM, created.operation_id)
            audit = session.get(SettingsOperationORM, created.operation_id)
            assert setting is not None
            assert setting.match_announcement_channel_id == CHANNEL_ID
            assert setting.win5_announcement_channel_id is None
            assert setting.log_channel_id == "203"
            assert setting.default_timezone == "Asia/Seoul"
            assert setting.match_odds_refresh_mode == "live"
            assert setting.match_odds_refresh_sequence == 17
            assert setting.match_odds_last_projection_fingerprint == "a" * 64
            assert first is not None and (first.status, first.target_channel_id) == ("ready", CHANNEL_ID)
            assert second is not None and (second.status, second.target_channel_id) == ("awaiting_channel", None)
            assert operation is not None
            assert operation.actor_discord_user_id == "900"
            assert operation.reason == "Automatic Discord publication channel provisioning."
            assert audit is not None and audit.type == DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE
            assert audit.before_data["publication"]["publication_id"] == 71
            assert audit.after_data["publication_id"] == 71
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, SettingsOperationORM) == 1
    finally:
        runtime.dispose()


def test_bind_supports_win5_announcement_destination() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        with runtime.session_factory.begin() as session:
            for publication_id in (71, 72):
                publication = session.get(DiscordPublicationORM, publication_id)
                assert publication is not None
                publication.destination_kind = WIN5_ANNOUNCEMENT_DESTINATION_KIND
                publication.event_type = WIN5_ROUND_RESULT_EVENT_TYPE
                publication.source_kind = "win5_round"

        target = compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=GUILD_ID)
        assert target is not None
        assert target.channel_name == "umabot-win5"
        command = ProvisionDiscordPublicationChannel(
            target=target,
            target_channel_id=CHANNEL_ID,
            actor_discord_user_id="900",
            idempotency_key="automatic-win5-channel-71",
        )

        result = compose_discord_publication_channel_provisioning_commands(runtime).provision(command)

        assert result.destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND
        with runtime.session_factory() as session:
            setting = session.get(BotGuildSettingORM, GUILD_ID)
            publication = session.get(DiscordPublicationORM, 71)
            assert setting is not None
            assert setting.win5_announcement_channel_id == CHANNEL_ID
            assert setting.match_announcement_channel_id is None
            assert publication is not None
            assert (publication.status, publication.target_channel_id) == ("ready", CHANNEL_ID)
    finally:
        runtime.dispose()


@pytest.mark.parametrize(
    ("enabled", "channel_id"),
    [(False, None), (True, "777")],
)
def test_query_excludes_disabled_or_already_configured_destination(
    enabled: bool,
    channel_id: str | None,
) -> None:
    runtime = _runtime()
    try:
        _seed(runtime, enabled=enabled, channel_id=channel_id)

        target = compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=GUILD_ID)

        assert target is None
        assert _count(runtime, OperationORM) == 0
    finally:
        runtime.dispose()


def test_audit_failure_rolls_back_settings_publication_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="automatic-channel-failure")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, SettingsOperationORM) for item in session.new):
                raise RuntimeError("simulated channel provisioning audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated channel provisioning audit failure"):
                compose_discord_publication_channel_provisioning_commands(runtime).provision(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            setting = session.get(BotGuildSettingORM, GUILD_ID)
            first = session.get(DiscordPublicationORM, 71)
            second = session.get(DiscordPublicationORM, 72)
            assert setting is not None and setting.match_announcement_channel_id is None
            assert first is not None and (first.status, first.target_channel_id) == ("awaiting_channel", None)
            assert second is not None and (second.status, second.target_channel_id) == ("awaiting_channel", None)
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, SettingsOperationORM) == 0
    finally:
        runtime.dispose()


def test_query_rejects_malformed_stored_target() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        with runtime.session_factory.begin() as session:
            publication = session.get(DiscordPublicationORM, 71)
            assert publication is not None
            publication.payload_fingerprint = "malformed"

        with pytest.raises(
            DiscordPublicationChannelProvisioningInvalidSourceError,
            match="target is malformed",
        ):
            compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=GUILD_ID)
    finally:
        runtime.dispose()
