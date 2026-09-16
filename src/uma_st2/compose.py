"""Composition Root for the UMA-ST-2 2.x runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from uma_st2.adapters.discord import (
    AccountCommandGroup,
    AccountRegistrationDiscordAdapter,
    AccountStatusDiscordAdapter,
    CirclePointExportDiscordAdapter,
    DiscordCommandGate,
    DiscordPublicationChannelProvisioningWorker,
    DiscordPublicationSenderClient,
    DiscordRuntimePreflight,
    ExportCommandGroup,
    ExportMatchCommandGroup,
    ExportWin5CommandGroup,
    MatchBettingCloseDiscordAdapter,
    MatchBettingOpenDiscordAdapter,
    MatchCancellationDiscordAdapter,
    MatchCommandGroup,
    MatchEntryDiscordAdapter,
    MatchMemberBettingDiscordAdapter,
    MatchOddsModeDiscordAdapter,
    MatchOddsPublicationRuntimeWorker,
    MatchRatingDiscordAdapter,
    MatchResultConfirmationDiscordAdapter,
    MatchResultPublicationDiscordAdapter,
    MatchResultReviewDiscordAdapter,
    MatchResultSubmissionDiscordAdapter,
    MatchSeasonExportDiscordAdapter,
    MatchSettlementDiscordAdapter,
    MatchSettlementRollbackDiscordAdapter,
    MatchSetupDiscordAdapter,
    MatchStaffCommandGroup,
    MatchStaffWorkflowDiscordAdapter,
    PublicationDeliveryScheduler,
    PublicationDeliveryWorker,
    PublicationDeliveryWorkerConfig,
    SettingsDiscordAdapter,
    StaffCirclePointDiscordAdapter,
    StaffCommandGroup,
    StaffDirectRegistrationDiscordAdapter,
    StaffDiscordAttachDiscordAdapter,
    StaffDisplayEditDiscordAdapter,
    StaffGameAccountAddDiscordAdapter,
    StaffGameAccountOwnerCorrectionDiscordAdapter,
    StaffPersonaDiscordAdapter,
    StaffPersonaStatusDiscordAdapter,
    UmaSt2DiscordClient,
    Win5MemberCommandGroup,
    Win5MemberDiscordAdapter,
    Win5SeasonExportDiscordAdapter,
    Win5StaffCommandGroup,
    Win5StaffDiscordAdapter,
    create_settings_command,
    validate_discord_runtime_settings,
)
from uma_st2.adapters.discord.common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    PrepareDiscordCommand,
)
from uma_st2.application.betting import (
    BetPlacementCommands,
    BetPlacementUnitOfWork,
    BetReplacementCommands,
    BetReplacementUnitOfWork,
    MatchMemberBettingQueries,
    MatchMemberBettingQueryUnitOfWork,
)
from uma_st2.application.discord import (
    DiscordGuildProvisioningCommands,
    DiscordGuildProvisioningUnitOfWork,
    DiscordGuildSettingsQueries,
    DiscordGuildSettingsQueryUnitOfWork,
    DiscordGuildSettingsUpdateCommands,
    DiscordGuildSettingsUpdateQueries,
    DiscordGuildSettingsUpdateQueryUnitOfWork,
    DiscordGuildSettingsUpdateUnitOfWork,
    DiscordPublicationChannelProvisioningCommands,
    DiscordPublicationChannelProvisioningQueries,
    DiscordPublicationChannelProvisioningQueryUnitOfWork,
    DiscordPublicationChannelProvisioningUnitOfWork,
)
from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWorkFactory
from uma_st2.application.exporting import (
    CirclePointExports,
    CirclePointExportUnitOfWork,
    MatchSeasonExports,
    MatchSeasonExportUnitOfWork,
    Win5SeasonExports,
    Win5SeasonExportUnitOfWork,
)
from uma_st2.application.identity import (
    AccountRegistrationCommands,
    AccountRegistrationUnitOfWork,
    AccountStatusQueries,
    AccountStatusQueryUnitOfWork,
    RegistrationReviewCommands,
    RegistrationReviewUnitOfWork,
    StaffDirectRegistrationCommands,
    StaffDirectRegistrationQueries,
    StaffDirectRegistrationQueryUnitOfWork,
    StaffDirectRegistrationUnitOfWork,
    StaffDiscordAttachCommands,
    StaffDiscordAttachQueries,
    StaffDiscordAttachQueryUnitOfWork,
    StaffDiscordAttachUnitOfWork,
    StaffDisplayEditCommands,
    StaffDisplayEditQueries,
    StaffDisplayEditQueryUnitOfWork,
    StaffDisplayEditUnitOfWork,
    StaffGameAccountAddCommands,
    StaffGameAccountAddQueries,
    StaffGameAccountAddQueryUnitOfWork,
    StaffGameAccountAddUnitOfWork,
    StaffGameAccountOwnerCorrectionCommands,
    StaffGameAccountOwnerCorrectionQueries,
    StaffGameAccountOwnerCorrectionQueryUnitOfWork,
    StaffGameAccountOwnerCorrectionUnitOfWork,
    StaffPersonaStatusCommands,
    StaffPersonaStatusQueries,
    StaffPersonaStatusQueryUnitOfWork,
    StaffPersonaStatusUnitOfWork,
    StaffRegistrationReviewQueries,
    StaffRegistrationReviewQueryUnitOfWork,
)
from uma_st2.application.master_data import MasterDataSeedCommands, MasterDataSeedUnitOfWork
from uma_st2.application.match import (
    MatchBettingCloseCommands,
    MatchBettingCloseUnitOfWork,
    MatchBettingOpenCommands,
    MatchBettingOpenUnitOfWork,
    MatchCancellationCommands,
    MatchCancellationUnitOfWork,
    MatchConditionCommands,
    MatchConditionUnitOfWork,
    MatchCreationCommands,
    MatchCreationUnitOfWork,
    MatchEntryCommands,
    MatchEntryUnitOfWork,
    MatchOddsPublicationCommands,
    MatchOddsPublicationQueries,
    MatchOddsPublicationQueryUnitOfWork,
    MatchOddsPublicationUnitOfWork,
    MatchResultConfirmationCommands,
    MatchResultConfirmationUnitOfWork,
    MatchResultPublicationCommands,
    MatchResultPublicationQueries,
    MatchResultPublicationQueryUnitOfWork,
    MatchResultPublicationUnitOfWork,
    MatchResultRejectionCommands,
    MatchResultRejectionUnitOfWork,
    MatchResultReviewQueries,
    MatchResultReviewQueryUnitOfWork,
    MatchResultSubmissionCommands,
    MatchResultSubmissionUnitOfWork,
    MatchSettlementCommands,
    MatchSettlementQueries,
    MatchSettlementQueryUnitOfWork,
    MatchSettlementRollbackCommands,
    MatchSettlementRollbackQueries,
    MatchSettlementRollbackQueryUnitOfWork,
    MatchSettlementRollbackUnitOfWork,
    MatchSettlementUnitOfWork,
    MatchSetupCommands,
    MatchSetupUnitOfWork,
    MatchStaffBettingCloseQueries,
    MatchStaffBettingCloseQueryUnitOfWork,
    MatchStaffBettingOpenQueries,
    MatchStaffBettingOpenQueryUnitOfWork,
    MatchStaffCancellationQueries,
    MatchStaffCancellationQueryUnitOfWork,
    MatchStaffConditionQueries,
    MatchStaffConditionQueryUnitOfWork,
    MatchStaffCreationQueries,
    MatchStaffCreationQueryUnitOfWork,
    MatchStaffEntryQueries,
    MatchStaffEntryQueryUnitOfWork,
    MatchStaffResultSubmissionQueries,
    MatchStaffResultSubmissionQueryUnitOfWork,
    MatchStaffSetupQueries,
    MatchStaffSetupQueryUnitOfWork,
)
from uma_st2.application.point import (
    StaffCirclePointCommands,
    StaffCirclePointQueries,
    StaffCirclePointQueryUnitOfWork,
    StaffCirclePointUnitOfWork,
)
from uma_st2.application.publication import (
    PublicationDeliveryCommands,
    PublicationDeliveryQueries,
    PublicationDeliveryQueryUnitOfWork,
    PublicationDeliveryUnitOfWork,
)
from uma_st2.application.rating import (
    MatchRatingQueries,
    MatchRatingQueryUnitOfWork,
    RatingRuleSeedCommands,
    RatingRuleSeedUnitOfWork,
)
from uma_st2.application.win5 import (
    Win5MemberCommands,
    Win5MemberCommandUnitOfWork,
    Win5MemberQueries,
    Win5MemberQueryUnitOfWork,
    Win5NormalResultCommands,
    Win5NormalResultUnitOfWork,
    Win5NormalScoringCommands,
    Win5NormalScoringUnitOfWork,
    Win5RoundCreationCommands,
    Win5RoundCreationUnitOfWork,
    Win5RoundLifecycleCommands,
    Win5RoundLifecycleUnitOfWork,
    Win5SeasonLifecycleCommands,
    Win5SeasonLifecycleUnitOfWork,
    Win5SetupRoundDeletionCommands,
    Win5SetupRoundDeletionUnitOfWork,
    Win5SpecialResultCommands,
    Win5SpecialResultUnitOfWork,
    Win5SpecialScoringCommands,
    Win5SpecialScoringUnitOfWork,
    Win5SpecialVoidCommands,
    Win5SpecialVoidUnitOfWork,
    Win5StaffResultQueries,
    Win5StaffResultQueryUnitOfWork,
    Win5StaffRoundCreationQueries,
    Win5StaffRoundCreationQueryUnitOfWork,
    Win5StaffRoundDeletionQueries,
    Win5StaffRoundDeletionQueryUnitOfWork,
    Win5StaffRoundLifecycleQueries,
    Win5StaffRoundLifecycleQueryUnitOfWork,
    Win5StaffSeasonQueries,
    Win5StaffSeasonQueryUnitOfWork,
    Win5StaffSpecialVoidQueries,
    Win5StaffSpecialVoidQueryUnitOfWork,
    Win5SubmissionHistoryQueries,
    Win5SubmissionHistoryQueryUnitOfWork,
)
from uma_st2.config import RuntimeSettings
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyAccountRegistrationUnitOfWorkFactory,
    SqlAlchemyAccountStatusQueryUnitOfWorkFactory,
    SqlAlchemyBetPlacementUnitOfWorkFactory,
    SqlAlchemyBetReplacementUnitOfWorkFactory,
    SqlAlchemyCirclePointExportUnitOfWorkFactory,
    SqlAlchemyDiscordGuildProvisioningUnitOfWorkFactory,
    SqlAlchemyDiscordGuildSettingsQueryUnitOfWorkFactory,
    SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWorkFactory,
    SqlAlchemyDiscordGuildSettingsUpdateUnitOfWorkFactory,
    SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWorkFactory,
    SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWorkFactory,
    SqlAlchemyMasterDataSeedUnitOfWorkFactory,
    SqlAlchemyMatchBettingCloseUnitOfWorkFactory,
    SqlAlchemyMatchBettingOpenUnitOfWorkFactory,
    SqlAlchemyMatchCancellationUnitOfWorkFactory,
    SqlAlchemyMatchConditionUnitOfWorkFactory,
    SqlAlchemyMatchCreationUnitOfWorkFactory,
    SqlAlchemyMatchEntryUnitOfWorkFactory,
    SqlAlchemyMatchMemberBettingQueryUnitOfWorkFactory,
    SqlAlchemyMatchOddsPublicationQueryUnitOfWorkFactory,
    SqlAlchemyMatchOddsPublicationUnitOfWorkFactory,
    SqlAlchemyMatchRatingQueryUnitOfWorkFactory,
    SqlAlchemyMatchResultConfirmationUnitOfWorkFactory,
    SqlAlchemyMatchResultPublicationQueryUnitOfWorkFactory,
    SqlAlchemyMatchResultPublicationUnitOfWorkFactory,
    SqlAlchemyMatchResultRejectionUnitOfWorkFactory,
    SqlAlchemyMatchResultReviewQueryUnitOfWorkFactory,
    SqlAlchemyMatchResultSubmissionUnitOfWorkFactory,
    SqlAlchemyMatchSeasonExportUnitOfWorkFactory,
    SqlAlchemyMatchSettlementQueryUnitOfWorkFactory,
    SqlAlchemyMatchSettlementRollbackQueryUnitOfWorkFactory,
    SqlAlchemyMatchSettlementRollbackUnitOfWorkFactory,
    SqlAlchemyMatchSettlementUnitOfWorkFactory,
    SqlAlchemyMatchSetupUnitOfWorkFactory,
    SqlAlchemyMatchStaffBettingCloseQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffBettingOpenQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffCancellationQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffConditionQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffCreationQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffEntryQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory,
    SqlAlchemyPublicationDeliveryUnitOfWorkFactory,
    SqlAlchemyRatingRuleSeedUnitOfWorkFactory,
    SqlAlchemyRegistrationReviewUnitOfWorkFactory,
    SqlAlchemyStaffCirclePointQueryUnitOfWorkFactory,
    SqlAlchemyStaffCirclePointUnitOfWorkFactory,
    SqlAlchemyStaffDirectRegistrationQueryUnitOfWorkFactory,
    SqlAlchemyStaffDirectRegistrationUnitOfWorkFactory,
    SqlAlchemyStaffDiscordAttachQueryUnitOfWorkFactory,
    SqlAlchemyStaffDiscordAttachUnitOfWorkFactory,
    SqlAlchemyStaffDisplayEditQueryUnitOfWorkFactory,
    SqlAlchemyStaffDisplayEditUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountAddQueryUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountAddUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWorkFactory,
    SqlAlchemyStaffPersonaStatusQueryUnitOfWorkFactory,
    SqlAlchemyStaffPersonaStatusUnitOfWorkFactory,
    SqlAlchemyStaffRegistrationReviewQueryUnitOfWorkFactory,
    SqlAlchemyWin5MemberCommandUnitOfWorkFactory,
    SqlAlchemyWin5MemberQueryUnitOfWorkFactory,
    SqlAlchemyWin5NormalResultUnitOfWorkFactory,
    SqlAlchemyWin5NormalScoringUnitOfWorkFactory,
    SqlAlchemyWin5RoundCreationUnitOfWorkFactory,
    SqlAlchemyWin5RoundLifecycleUnitOfWorkFactory,
    SqlAlchemyWin5SeasonExportUnitOfWorkFactory,
    SqlAlchemyWin5SeasonLifecycleUnitOfWorkFactory,
    SqlAlchemyWin5SetupRoundDeletionUnitOfWorkFactory,
    SqlAlchemyWin5SpecialResultUnitOfWorkFactory,
    SqlAlchemyWin5SpecialScoringUnitOfWorkFactory,
    SqlAlchemyWin5SpecialVoidUnitOfWorkFactory,
    SqlAlchemyWin5StaffResultQueryUnitOfWorkFactory,
    SqlAlchemyWin5StaffRoundCreationQueryUnitOfWorkFactory,
    SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWorkFactory,
    SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWorkFactory,
    SqlAlchemyWin5StaffSeasonQueryUnitOfWorkFactory,
    SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWorkFactory,
    SqlAlchemyWin5SubmissionHistoryQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.exporting import (
    CirclePointXlsxRenderer,
    MatchSeasonXlsxRenderer,
    Win5SeasonXlsxRenderer,
)
from uma_st2.shared import utc_now


def compose_match_creation(database_runtime: DatabaseRuntime) -> MatchCreationCommands:
    """Wire one native Match creation to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchCreationUnitOfWork] = SqlAlchemyMatchCreationUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchCreationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_master_data_seed_commands(database_runtime: DatabaseRuntime) -> MasterDataSeedCommands:
    """Wire one reviewed complete initial master-data seed to a fresh UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MasterDataSeedUnitOfWork] = SqlAlchemyMasterDataSeedUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MasterDataSeedCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_conditions(database_runtime: DatabaseRuntime) -> MatchConditionCommands:
    """Wire native Match condition mutation to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchConditionUnitOfWork] = SqlAlchemyMatchConditionUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchConditionCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_setup(database_runtime: DatabaseRuntime) -> MatchSetupCommands:
    """Wire complete native scheduled Match setup replacement to a fresh UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSetupUnitOfWork] = SqlAlchemyMatchSetupUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchSetupCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_entries(database_runtime: DatabaseRuntime) -> MatchEntryCommands:
    """Wire native Match Entry replacement to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchEntryUnitOfWork] = SqlAlchemyMatchEntryUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchEntryCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_betting_open(database_runtime: DatabaseRuntime) -> MatchBettingOpenCommands:
    """Wire native Match betting-open to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchBettingOpenUnitOfWork] = SqlAlchemyMatchBettingOpenUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchBettingOpenCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_betting_close(database_runtime: DatabaseRuntime) -> MatchBettingCloseCommands:
    """Wire native Match betting-close to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchBettingCloseUnitOfWork] = SqlAlchemyMatchBettingCloseUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchBettingCloseCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_cancellation(database_runtime: DatabaseRuntime) -> MatchCancellationCommands:
    """Wire native whole-Match cancellation to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchCancellationUnitOfWork] = SqlAlchemyMatchCancellationUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchCancellationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_settlement(database_runtime: DatabaseRuntime) -> MatchSettlementCommands:
    """Wire native Match settlement to one fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSettlementUnitOfWork] = SqlAlchemyMatchSettlementUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchSettlementCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_settlement_rollback(
    database_runtime: DatabaseRuntime,
) -> MatchSettlementRollbackCommands:
    """Wire terminal Match settlement rollback to one fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSettlementRollbackUnitOfWork] = (
        SqlAlchemyMatchSettlementRollbackUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchSettlementRollbackCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_result_submission(
    database_runtime: DatabaseRuntime,
) -> MatchResultSubmissionCommands:
    """Wire native pending Match result revisions to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultSubmissionUnitOfWork] = (
        SqlAlchemyMatchResultSubmissionUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultSubmissionCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_result_rejection(
    database_runtime: DatabaseRuntime,
) -> MatchResultRejectionCommands:
    """Wire current-pending Match result rejection to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultRejectionUnitOfWork] = (
        SqlAlchemyMatchResultRejectionUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultRejectionCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_result_confirmation(
    database_runtime: DatabaseRuntime,
) -> MatchResultConfirmationCommands:
    """Wire current-pending Match result confirmation to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultConfirmationUnitOfWork] = (
        SqlAlchemyMatchResultConfirmationUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultConfirmationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_result_publication(
    database_runtime: DatabaseRuntime,
) -> MatchResultPublicationCommands:
    """Wire settled-result publication recovery to one fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultPublicationUnitOfWork] = (
        SqlAlchemyMatchResultPublicationUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultPublicationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_odds_publication(
    database_runtime: DatabaseRuntime,
) -> MatchOddsPublicationCommands:
    """Wire periodic Match odds generation/mode mutation to fresh UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchOddsPublicationUnitOfWork] = (
        SqlAlchemyMatchOddsPublicationUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchOddsPublicationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_odds_publication_queries(
    database_runtime: DatabaseRuntime,
) -> MatchOddsPublicationQueries:
    """Wire private current periodic-odds status to read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchOddsPublicationQueryUnitOfWork] = (
        SqlAlchemyMatchOddsPublicationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchOddsPublicationQueries(QueryRunner(unit_of_work_factory))


def compose_bet_placement(database_runtime: DatabaseRuntime) -> BetPlacementCommands:
    """Wire native member Bet placement to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[BetPlacementUnitOfWork] = SqlAlchemyBetPlacementUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return BetPlacementCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_bet_replacement(database_runtime: DatabaseRuntime) -> BetReplacementCommands:
    """Wire native member Bet replacement to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[BetReplacementUnitOfWork] = SqlAlchemyBetReplacementUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return BetReplacementCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_match_staff_creation_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffCreationQueries:
    """Wire current master-course choices to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffCreationQueryUnitOfWork] = (
        SqlAlchemyMatchStaffCreationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffCreationQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_condition_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffConditionQueries:
    """Wire eligible Match condition targets to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffConditionQueryUnitOfWork] = (
        SqlAlchemyMatchStaffConditionQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffConditionQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_setup_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffSetupQueries:
    """Wire editable Match setup projections to fresh read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffSetupQueryUnitOfWork] = (
        SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffSetupQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_entry_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffEntryQueries:
    """Wire eligible Match Entry targets and exact lookup projections."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffEntryQueryUnitOfWork] = (
        SqlAlchemyMatchStaffEntryQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffEntryQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_betting_open_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffBettingOpenQueries:
    """Wire native scheduled opening targets to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffBettingOpenQueryUnitOfWork] = (
        SqlAlchemyMatchStaffBettingOpenQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffBettingOpenQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_betting_close_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffBettingCloseQueries:
    """Wire native betting-open close targets to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffBettingCloseQueryUnitOfWork] = (
        SqlAlchemyMatchStaffBettingCloseQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffBettingCloseQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_cancellation_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffCancellationQueries:
    """Wire native pre-settlement cancellation targets to read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffCancellationQueryUnitOfWork] = (
        SqlAlchemyMatchStaffCancellationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffCancellationQueries(QueryRunner(unit_of_work_factory))


def compose_match_settlement_queries(
    database_runtime: DatabaseRuntime,
) -> MatchSettlementQueries:
    """Wire settlement Preview/target queries to fresh read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSettlementQueryUnitOfWork] = (
        SqlAlchemyMatchSettlementQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchSettlementQueries(QueryRunner(unit_of_work_factory))


def compose_match_settlement_rollback_queries(
    database_runtime: DatabaseRuntime,
) -> MatchSettlementRollbackQueries:
    """Wire terminal rollback Preview queries to fresh read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSettlementRollbackQueryUnitOfWork] = (
        SqlAlchemyMatchSettlementRollbackQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchSettlementRollbackQueries(QueryRunner(unit_of_work_factory))


def compose_match_staff_result_submission_queries(
    database_runtime: DatabaseRuntime,
) -> MatchStaffResultSubmissionQueries:
    """Wire eligible Match result targets to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchStaffResultSubmissionQueryUnitOfWork] = (
        SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchStaffResultSubmissionQueries(QueryRunner(unit_of_work_factory))


def compose_match_result_review_queries(
    database_runtime: DatabaseRuntime,
) -> MatchResultReviewQueries:
    """Wire current-pending Match result review to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultReviewQueryUnitOfWork] = (
        SqlAlchemyMatchResultReviewQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultReviewQueries(QueryRunner(unit_of_work_factory))


def compose_match_result_publication_queries(
    database_runtime: DatabaseRuntime,
) -> MatchResultPublicationQueries:
    """Wire native settled publication autocomplete to read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchResultPublicationQueryUnitOfWork] = (
        SqlAlchemyMatchResultPublicationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchResultPublicationQueries(QueryRunner(unit_of_work_factory))


def compose_match_member_betting_queries(
    database_runtime: DatabaseRuntime,
) -> MatchMemberBettingQueries:
    """Wire member betting-open Match choices to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchMemberBettingQueryUnitOfWork] = (
        SqlAlchemyMatchMemberBettingQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return MatchMemberBettingQueries(QueryRunner(unit_of_work_factory))


def compose_match_rating_queries(
    database_runtime: DatabaseRuntime,
) -> MatchRatingQueries:
    """Wire current Rating standings to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[MatchRatingQueryUnitOfWork] = SqlAlchemyMatchRatingQueryUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchRatingQueries(QueryRunner(unit_of_work_factory))


def compose_account_status_queries(
    database_runtime: DatabaseRuntime,
) -> AccountStatusQueries:
    """Wire private Account status tabs to fresh read-only query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[AccountStatusQueryUnitOfWork] = (
        SqlAlchemyAccountStatusQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return AccountStatusQueries(QueryRunner(unit_of_work_factory), clock=utc_now)


def compose_account_registration_commands(
    database_runtime: DatabaseRuntime,
) -> AccountRegistrationCommands:
    """Wire direct-final Account registration requests to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[AccountRegistrationUnitOfWork] = (
        SqlAlchemyAccountRegistrationUnitOfWorkFactory(database_runtime.session_factory)
    )
    return AccountRegistrationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_rating_rule_seed_commands(
    database_runtime: DatabaseRuntime,
) -> RatingRuleSeedCommands:
    """Wire one reviewed workbook seed to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[RatingRuleSeedUnitOfWork] = SqlAlchemyRatingRuleSeedUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return RatingRuleSeedCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_discord_guild_settings_queries(
    database_runtime: DatabaseRuntime,
) -> DiscordGuildSettingsQueries:
    """Wire fresh runtime authorization settings reads to query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordGuildSettingsQueryUnitOfWork] = (
        SqlAlchemyDiscordGuildSettingsQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordGuildSettingsQueries(QueryRunner(unit_of_work_factory))


def compose_discord_guild_settings_update_queries(
    database_runtime: DatabaseRuntime,
) -> DiscordGuildSettingsUpdateQueries:
    """Wire detached settings editor reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordGuildSettingsUpdateQueryUnitOfWork] = (
        SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordGuildSettingsUpdateQueries(QueryRunner(unit_of_work_factory))


def compose_discord_guild_settings_update_commands(
    database_runtime: DatabaseRuntime,
) -> DiscordGuildSettingsUpdateCommands:
    """Wire audited runtime settings updates to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordGuildSettingsUpdateUnitOfWork] = (
        SqlAlchemyDiscordGuildSettingsUpdateUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordGuildSettingsUpdateCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_discord_publication_channel_provisioning_queries(
    database_runtime: DatabaseRuntime,
) -> DiscordPublicationChannelProvisioningQueries:
    """Wire one detached oldest automatic channel target query."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordPublicationChannelProvisioningQueryUnitOfWork] = (
        SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordPublicationChannelProvisioningQueries(QueryRunner(unit_of_work_factory))


def compose_discord_publication_channel_provisioning_commands(
    database_runtime: DatabaseRuntime,
) -> DiscordPublicationChannelProvisioningCommands:
    """Wire one provider-success settings/publication atomic bind."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordPublicationChannelProvisioningUnitOfWork] = (
        SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordPublicationChannelProvisioningCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_discord_guild_provisioning(
    database_runtime: DatabaseRuntime,
) -> DiscordGuildProvisioningCommands:
    """Wire create-only reviewed guild settings provisioning."""

    unit_of_work_factory: UnitOfWorkFactory[DiscordGuildProvisioningUnitOfWork] = (
        SqlAlchemyDiscordGuildProvisioningUnitOfWorkFactory(database_runtime.session_factory)
    )
    return DiscordGuildProvisioningCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_publication_delivery(
    database_runtime: DatabaseRuntime,
) -> PublicationDeliveryCommands:
    """Wire supported publication claim/finalize operations to fresh command UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[PublicationDeliveryUnitOfWork] = (
        SqlAlchemyPublicationDeliveryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return PublicationDeliveryCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_publication_delivery_queries(
    database_runtime: DatabaseRuntime,
) -> PublicationDeliveryQueries:
    """Wire bounded unknown-delivery inspection to a fresh read-only UoW."""

    unit_of_work_factory: UnitOfWorkFactory[PublicationDeliveryQueryUnitOfWork] = (
        SqlAlchemyPublicationDeliveryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return PublicationDeliveryQueries(QueryRunner(unit_of_work_factory))


def compose_win5_member_commands(database_runtime: DatabaseRuntime) -> Win5MemberCommands:
    """Wire member WIN5 mutations to a fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5MemberCommandUnitOfWork] = SqlAlchemyWin5MemberCommandUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5MemberCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_member_queries(database_runtime: DatabaseRuntime) -> Win5MemberQueries:
    """Wire the member WIN5 query boundary to the runtime Session factory."""

    unit_of_work_factory: UnitOfWorkFactory[Win5MemberQueryUnitOfWork] = SqlAlchemyWin5MemberQueryUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5MemberQueries(QueryRunner(unit_of_work_factory))


def compose_win5_submission_history_queries(
    database_runtime: DatabaseRuntime,
) -> Win5SubmissionHistoryQueries:
    """Wire owned WIN5 Submission history to its query capability."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SubmissionHistoryQueryUnitOfWork] = (
        SqlAlchemyWin5SubmissionHistoryQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5SubmissionHistoryQueries(QueryRunner(unit_of_work_factory))


def compose_win5_season_exports(database_runtime: DatabaseRuntime) -> Win5SeasonExports:
    """Wire complete WIN5 Season snapshots to workbook v1 rendering."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SeasonExportUnitOfWork] = SqlAlchemyWin5SeasonExportUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5SeasonExports(
        QueryRunner(unit_of_work_factory),
        Win5SeasonXlsxRenderer(),
        clock=utc_now,
    )


def compose_circle_point_exports(database_runtime: DatabaseRuntime) -> CirclePointExports:
    """Wire current Circle Point snapshots to workbook v1 rendering."""

    unit_of_work_factory: UnitOfWorkFactory[CirclePointExportUnitOfWork] = SqlAlchemyCirclePointExportUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return CirclePointExports(
        QueryRunner(unit_of_work_factory),
        CirclePointXlsxRenderer(),
        clock=utc_now,
    )


def compose_match_season_exports(database_runtime: DatabaseRuntime) -> MatchSeasonExports:
    """Wire complete Circle Match Season snapshots to workbook v1 rendering."""

    unit_of_work_factory: UnitOfWorkFactory[MatchSeasonExportUnitOfWork] = SqlAlchemyMatchSeasonExportUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return MatchSeasonExports(
        QueryRunner(unit_of_work_factory),
        MatchSeasonXlsxRenderer(),
        clock=utc_now,
    )


def compose_win5_normal_scoring(database_runtime: DatabaseRuntime) -> Win5NormalScoringCommands:
    """Wire Normal WIN5 scoring to one fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5NormalScoringUnitOfWork] = SqlAlchemyWin5NormalScoringUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5NormalScoringCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_normal_results(database_runtime: DatabaseRuntime) -> Win5NormalResultCommands:
    """Wire Normal WIN5 result entry/correction to a fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5NormalResultUnitOfWork] = SqlAlchemyWin5NormalResultUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5NormalResultCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_special_results(database_runtime: DatabaseRuntime) -> Win5SpecialResultCommands:
    """Wire Special WIN5 result entry/correction to a fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SpecialResultUnitOfWork] = SqlAlchemyWin5SpecialResultUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5SpecialResultCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_special_scoring(database_runtime: DatabaseRuntime) -> Win5SpecialScoringCommands:
    """Wire Special WIN5 scoring to one fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SpecialScoringUnitOfWork] = (
        SqlAlchemyWin5SpecialScoringUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5SpecialScoringCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_special_voids(database_runtime: DatabaseRuntime) -> Win5SpecialVoidCommands:
    """Wire Special Race void/restore to one fresh application-owned UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SpecialVoidUnitOfWork] = SqlAlchemyWin5SpecialVoidUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5SpecialVoidCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_staff_result_queries(database_runtime: DatabaseRuntime) -> Win5StaffResultQueries:
    """Wire staff Normal/Special result selectors to one fresh read-only UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffResultQueryUnitOfWork] = (
        SqlAlchemyWin5StaffResultQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffResultQueries(QueryRunner(unit_of_work_factory))


def compose_win5_staff_special_void_queries(
    database_runtime: DatabaseRuntime,
) -> Win5StaffSpecialVoidQueries:
    """Wire staff Special void selectors to one fresh read-only UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffSpecialVoidQueryUnitOfWork] = (
        SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffSpecialVoidQueries(QueryRunner(unit_of_work_factory))


def compose_win5_round_lifecycle(database_runtime: DatabaseRuntime) -> Win5RoundLifecycleCommands:
    """Wire one Round open/close transition to one fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5RoundLifecycleUnitOfWork] = (
        SqlAlchemyWin5RoundLifecycleUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5RoundLifecycleCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_season_lifecycle(database_runtime: DatabaseRuntime) -> Win5SeasonLifecycleCommands:
    """Wire one Season mutation to one fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SeasonLifecycleUnitOfWork] = (
        SqlAlchemyWin5SeasonLifecycleUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5SeasonLifecycleCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_round_creation(database_runtime: DatabaseRuntime) -> Win5RoundCreationCommands:
    """Wire one Normal/Special Round graph creation to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5RoundCreationUnitOfWork] = SqlAlchemyWin5RoundCreationUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return Win5RoundCreationCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_setup_round_deletion(
    database_runtime: DatabaseRuntime,
) -> Win5SetupRoundDeletionCommands:
    """Wire one guarded setup-Round deletion to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[Win5SetupRoundDeletionUnitOfWork] = (
        SqlAlchemyWin5SetupRoundDeletionUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5SetupRoundDeletionCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_win5_staff_round_creation_queries(
    database_runtime: DatabaseRuntime,
) -> Win5StaffRoundCreationQueries:
    """Wire paged Round-creation Season choices to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffRoundCreationQueryUnitOfWork] = (
        SqlAlchemyWin5StaffRoundCreationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffRoundCreationQueries(QueryRunner(unit_of_work_factory))


def compose_win5_staff_round_deletion_queries(
    database_runtime: DatabaseRuntime,
) -> Win5StaffRoundDeletionQueries:
    """Wire paged setup-Round deletion projections to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffRoundDeletionQueryUnitOfWork] = (
        SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffRoundDeletionQueries(QueryRunner(unit_of_work_factory))


def compose_win5_staff_round_lifecycle_queries(
    database_runtime: DatabaseRuntime,
) -> Win5StaffRoundLifecycleQueries:
    """Wire paged staff Round lifecycle projections to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffRoundLifecycleQueryUnitOfWork] = (
        SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffRoundLifecycleQueries(QueryRunner(unit_of_work_factory))


def compose_win5_staff_season_queries(
    database_runtime: DatabaseRuntime,
) -> Win5StaffSeasonQueries:
    """Wire paged staff Season projections to fresh read-only UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[Win5StaffSeasonQueryUnitOfWork] = (
        SqlAlchemyWin5StaffSeasonQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return Win5StaffSeasonQueries(QueryRunner(unit_of_work_factory))


def compose_win5_staff_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> Win5StaffCommandGroup:
    """Wire the implemented Round creation/deletion/lifecycle/result/scoring slice to `/win5 staff`."""

    adapter = Win5StaffDiscordAdapter(
        queries=compose_win5_staff_result_queries(database_runtime),
        commands=compose_win5_normal_results(database_runtime),
        special_commands=compose_win5_special_results(database_runtime),
        scoring_commands=compose_win5_normal_scoring(database_runtime),
        special_scoring_commands=compose_win5_special_scoring(database_runtime),
        special_void_queries=compose_win5_staff_special_void_queries(database_runtime),
        special_void_commands=compose_win5_special_voids(database_runtime),
        creation_queries=compose_win5_staff_round_creation_queries(database_runtime),
        creation_commands=compose_win5_round_creation(database_runtime),
        lifecycle_queries=compose_win5_staff_round_lifecycle_queries(database_runtime),
        lifecycle_commands=compose_win5_round_lifecycle(database_runtime),
        season_queries=compose_win5_staff_season_queries(database_runtime),
        season_commands=compose_win5_season_lifecycle(database_runtime),
        deletion_queries=compose_win5_staff_round_deletion_queries(database_runtime),
        deletion_commands=compose_win5_setup_round_deletion(database_runtime),
        authorize_interaction=authorize_interaction,
        prepare_command=prepare_command,
    )
    return Win5StaffCommandGroup(adapter=adapter)


def compose_win5_member_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_autocomplete: AuthorizeDiscordAutocomplete,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> Win5MemberCommandGroup:
    """Wire the current member WIN5 Discord leaf to bounded application ports."""

    adapter = Win5MemberDiscordAdapter(
        queries=compose_win5_member_queries(database_runtime),
        submission_history_queries=compose_win5_submission_history_queries(database_runtime),
        commands=compose_win5_member_commands(database_runtime),
        prepare_command=prepare_command,
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    return Win5MemberCommandGroup(adapter=adapter)


def compose_win5_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_autocomplete: AuthorizeDiscordAutocomplete,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> Win5MemberCommandGroup:
    """Compose the incremental V2 `/win5` member root and staff subgroup."""

    group = compose_win5_member_command_group(
        database_runtime,
        prepare_command=prepare_command,
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    group.add_command(
        compose_win5_staff_command_group(
            database_runtime,
            prepare_command=prepare_command,
            authorize_interaction=authorize_interaction,
        )
    )
    return group


def compose_export_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_autocomplete: AuthorizeDiscordAutocomplete,
) -> ExportCommandGroup:
    """Compose current domain-specific `/export` attachment surfaces."""

    win5_adapter = Win5SeasonExportDiscordAdapter(
        exports=compose_win5_season_exports(database_runtime),
        prepare_command=prepare_command,
        authorize_autocomplete=authorize_autocomplete,
    )
    match_adapter = MatchSeasonExportDiscordAdapter(
        exports=compose_match_season_exports(database_runtime),
        prepare_command=prepare_command,
        authorize_autocomplete=authorize_autocomplete,
    )
    circle_point_adapter = CirclePointExportDiscordAdapter(
        exports=compose_circle_point_exports(database_runtime),
        prepare_command=prepare_command,
    )
    return ExportCommandGroup(
        circle_point_adapter=circle_point_adapter,
        win5_group=ExportWin5CommandGroup(adapter=win5_adapter),
        match_group=ExportMatchCommandGroup(adapter=match_adapter),
    )


def compose_match_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_autocomplete: AuthorizeDiscordAutocomplete,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> MatchCommandGroup:
    """Compose current native Match member and staff vertical slices."""

    entry_adapter = MatchEntryDiscordAdapter(
        queries=compose_match_staff_entry_queries(database_runtime),
        commands=compose_match_entries(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    setup_adapter = MatchSetupDiscordAdapter(
        creation_queries=compose_match_staff_creation_queries(database_runtime),
        setup_queries=compose_match_staff_setup_queries(database_runtime),
        creation_commands=compose_match_creation(database_runtime),
        setup_commands=compose_match_setup(database_runtime),
        entry_adapter=entry_adapter,
        authorize_interaction=authorize_interaction,
    )
    betting_open_adapter = MatchBettingOpenDiscordAdapter(
        queries=compose_match_staff_betting_open_queries(database_runtime),
        commands=compose_match_betting_open(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    betting_close_adapter = MatchBettingCloseDiscordAdapter(
        queries=compose_match_staff_betting_close_queries(database_runtime),
        commands=compose_match_betting_close(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    result_submission_adapter = MatchResultSubmissionDiscordAdapter(
        queries=compose_match_staff_result_submission_queries(database_runtime),
        commands=compose_match_result_submission(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    result_review_queries = compose_match_result_review_queries(database_runtime)
    result_review_adapter = MatchResultReviewDiscordAdapter(
        queries=result_review_queries,
        commands=compose_match_result_rejection(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    result_confirmation_adapter = MatchResultConfirmationDiscordAdapter(
        queries=result_review_queries,
        commands=compose_match_result_confirmation(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    cancellation_adapter = MatchCancellationDiscordAdapter(
        queries=compose_match_staff_cancellation_queries(database_runtime),
        commands=compose_match_cancellation(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    settlement_adapter = MatchSettlementDiscordAdapter(
        queries=compose_match_settlement_queries(database_runtime),
        commands=compose_match_settlement(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    settlement_rollback_adapter = MatchSettlementRollbackDiscordAdapter(
        queries=compose_match_settlement_rollback_queries(database_runtime),
        commands=compose_match_settlement_rollback(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    publication_adapter = MatchResultPublicationDiscordAdapter(
        queries=compose_match_result_publication_queries(database_runtime),
        commands=compose_match_result_publication(database_runtime),
        authorize_autocomplete=authorize_autocomplete,
        authorize_interaction=authorize_interaction,
    )
    odds_mode_adapter = MatchOddsModeDiscordAdapter(
        queries=compose_match_odds_publication_queries(database_runtime),
        commands=compose_match_odds_publication(database_runtime),
        authorize_interaction=authorize_interaction,
    )
    member_betting_adapter = MatchMemberBettingDiscordAdapter(
        queries=compose_match_member_betting_queries(database_runtime),
        commands=compose_bet_placement(database_runtime),
        replacement_commands=compose_bet_replacement(database_runtime),
        prepare_command=prepare_command,
        authorize_autocomplete=authorize_autocomplete,
    )
    rating_adapter = MatchRatingDiscordAdapter(
        queries=compose_match_rating_queries(database_runtime),
        prepare_command=prepare_command,
    )
    workflow_adapter = MatchStaffWorkflowDiscordAdapter(
        setup_adapter=setup_adapter,
        betting_open_adapter=betting_open_adapter,
        betting_close_adapter=betting_close_adapter,
        cancellation_adapter=cancellation_adapter,
        result_submission_adapter=result_submission_adapter,
        result_review_adapter=result_review_adapter,
        result_confirmation_adapter=result_confirmation_adapter,
        settlement_adapter=settlement_adapter,
        settlement_rollback_adapter=settlement_rollback_adapter,
        publication_adapter=publication_adapter,
        odds_mode_adapter=odds_mode_adapter,
        prepare_command=prepare_command,
        authorize_interaction=authorize_interaction,
    )
    return MatchCommandGroup(
        betting_adapter=member_betting_adapter,
        rating_adapter=rating_adapter,
        staff_group=MatchStaffCommandGroup(adapter=workflow_adapter),
    )


def compose_account_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> AccountCommandGroup:
    """Compose private Account registration and status commands."""

    return AccountCommandGroup(
        status_adapter=AccountStatusDiscordAdapter(
            queries=compose_account_status_queries(database_runtime),
            prepare_command=prepare_command,
            authorize_interaction=authorize_interaction,
        ),
        registration_adapter=AccountRegistrationDiscordAdapter(
            commands=compose_account_registration_commands(database_runtime),
            prepare_command=prepare_command,
            authorize_interaction=authorize_interaction,
        ),
    )


def compose_staff_registration_review_queries(
    database_runtime: DatabaseRuntime,
) -> StaffRegistrationReviewQueries:
    """Wire private staff request/Persona projections to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffRegistrationReviewQueryUnitOfWork] = (
        SqlAlchemyStaffRegistrationReviewQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffRegistrationReviewQueries(QueryRunner(unit_of_work_factory))


def compose_registration_review_commands(
    database_runtime: DatabaseRuntime,
) -> RegistrationReviewCommands:
    """Wire one approval or rejection to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[RegistrationReviewUnitOfWork] = (
        SqlAlchemyRegistrationReviewUnitOfWorkFactory(database_runtime.session_factory)
    )
    return RegistrationReviewCommands(
        CommandRunner(unit_of_work_factory),
        clock=utc_now,
        persona_id_factory=lambda: str(uuid4()),
    )


def compose_staff_discord_attach_queries(
    database_runtime: DatabaseRuntime,
) -> StaffDiscordAttachQueries:
    """Wire direct-attach Preview reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDiscordAttachQueryUnitOfWork] = (
        SqlAlchemyStaffDiscordAttachQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffDiscordAttachQueries(QueryRunner(unit_of_work_factory))


def compose_staff_discord_attach_commands(
    database_runtime: DatabaseRuntime,
) -> StaffDiscordAttachCommands:
    """Wire one direct Discord access link to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDiscordAttachUnitOfWork] = (
        SqlAlchemyStaffDiscordAttachUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffDiscordAttachCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_direct_registration_queries(
    database_runtime: DatabaseRuntime,
) -> StaffDirectRegistrationQueries:
    """Wire direct-registration Preview reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDirectRegistrationQueryUnitOfWork] = (
        SqlAlchemyStaffDirectRegistrationQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffDirectRegistrationQueries(QueryRunner(unit_of_work_factory))


def compose_staff_direct_registration_commands(
    database_runtime: DatabaseRuntime,
) -> StaffDirectRegistrationCommands:
    """Wire one staff direct registration to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDirectRegistrationUnitOfWork] = (
        SqlAlchemyStaffDirectRegistrationUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffDirectRegistrationCommands(
        CommandRunner(unit_of_work_factory),
        clock=utc_now,
        persona_id_factory=lambda: str(uuid4()),
    )


def compose_staff_game_account_add_queries(
    database_runtime: DatabaseRuntime,
) -> StaffGameAccountAddQueries:
    """Wire peer GameAccount-add Preview reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffGameAccountAddQueryUnitOfWork] = (
        SqlAlchemyStaffGameAccountAddQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffGameAccountAddQueries(QueryRunner(unit_of_work_factory))


def compose_staff_game_account_add_commands(
    database_runtime: DatabaseRuntime,
) -> StaffGameAccountAddCommands:
    """Wire one peer GameAccount addition to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffGameAccountAddUnitOfWork] = (
        SqlAlchemyStaffGameAccountAddUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffGameAccountAddCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_game_account_owner_correction_queries(
    database_runtime: DatabaseRuntime,
) -> StaffGameAccountOwnerCorrectionQueries:
    """Wire GameAccount owner-correction Preview reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffGameAccountOwnerCorrectionQueryUnitOfWork] = (
        SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffGameAccountOwnerCorrectionQueries(QueryRunner(unit_of_work_factory))


def compose_staff_game_account_owner_correction_commands(
    database_runtime: DatabaseRuntime,
) -> StaffGameAccountOwnerCorrectionCommands:
    """Wire one GameAccount owner correction to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffGameAccountOwnerCorrectionUnitOfWork] = (
        SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffGameAccountOwnerCorrectionCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_display_edit_queries(
    database_runtime: DatabaseRuntime,
) -> StaffDisplayEditQueries:
    """Wire staff display-edit reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDisplayEditQueryUnitOfWork] = (
        SqlAlchemyStaffDisplayEditQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffDisplayEditQueries(QueryRunner(unit_of_work_factory))


def compose_staff_display_edit_commands(
    database_runtime: DatabaseRuntime,
) -> StaffDisplayEditCommands:
    """Wire one staff display correction to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffDisplayEditUnitOfWork] = SqlAlchemyStaffDisplayEditUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return StaffDisplayEditCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_persona_status_queries(
    database_runtime: DatabaseRuntime,
) -> StaffPersonaStatusQueries:
    """Wire staff Persona-status reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffPersonaStatusQueryUnitOfWork] = (
        SqlAlchemyStaffPersonaStatusQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffPersonaStatusQueries(QueryRunner(unit_of_work_factory))


def compose_staff_persona_status_commands(
    database_runtime: DatabaseRuntime,
) -> StaffPersonaStatusCommands:
    """Wire one staff Persona status change to a fresh Identity command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffPersonaStatusUnitOfWork] = (
        SqlAlchemyStaffPersonaStatusUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffPersonaStatusCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_circle_point_queries(
    database_runtime: DatabaseRuntime,
) -> StaffCirclePointQueries:
    """Wire staff Circle Point target/Preview reads to fresh query UoWs."""

    unit_of_work_factory: UnitOfWorkFactory[StaffCirclePointQueryUnitOfWork] = (
        SqlAlchemyStaffCirclePointQueryUnitOfWorkFactory(database_runtime.session_factory)
    )
    return StaffCirclePointQueries(QueryRunner(unit_of_work_factory))


def compose_staff_circle_point_commands(
    database_runtime: DatabaseRuntime,
) -> StaffCirclePointCommands:
    """Wire one Persona-owned manual Point delta to a fresh command UoW."""

    unit_of_work_factory: UnitOfWorkFactory[StaffCirclePointUnitOfWork] = SqlAlchemyStaffCirclePointUnitOfWorkFactory(
        database_runtime.session_factory
    )
    return StaffCirclePointCommands(CommandRunner(unit_of_work_factory), clock=utc_now)


def compose_staff_command_group(
    database_runtime: DatabaseRuntime,
    *,
    prepare_command: PrepareDiscordCommand,
    authorize_autocomplete: AuthorizeDiscordAutocomplete,
    authorize_interaction: AuthorizeDiscordInteraction,
) -> StaffCommandGroup:
    """Compose the current cross-domain staff root."""

    return StaffCommandGroup(
        circle_point_adapter=StaffCirclePointDiscordAdapter(
            queries=compose_staff_circle_point_queries(database_runtime),
            commands=compose_staff_circle_point_commands(database_runtime),
            authorize_autocomplete=authorize_autocomplete,
            authorize_interaction=authorize_interaction,
            prepare_command=prepare_command,
        ),
        persona_adapter=StaffPersonaDiscordAdapter(
            queries=compose_staff_registration_review_queries(database_runtime),
            commands=compose_registration_review_commands(database_runtime),
            attach_adapter=StaffDiscordAttachDiscordAdapter(
                queries=compose_staff_discord_attach_queries(database_runtime),
                commands=compose_staff_discord_attach_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            registration_adapter=StaffDirectRegistrationDiscordAdapter(
                queries=compose_staff_direct_registration_queries(database_runtime),
                commands=compose_staff_direct_registration_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            game_account_add_adapter=StaffGameAccountAddDiscordAdapter(
                queries=compose_staff_game_account_add_queries(database_runtime),
                commands=compose_staff_game_account_add_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            owner_correction_adapter=StaffGameAccountOwnerCorrectionDiscordAdapter(
                queries=compose_staff_game_account_owner_correction_queries(database_runtime),
                commands=compose_staff_game_account_owner_correction_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            display_edit_adapter=StaffDisplayEditDiscordAdapter(
                queries=compose_staff_display_edit_queries(database_runtime),
                commands=compose_staff_display_edit_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            status_adapter=StaffPersonaStatusDiscordAdapter(
                queries=compose_staff_persona_status_queries(database_runtime),
                commands=compose_staff_persona_status_commands(database_runtime),
                prepare_command=prepare_command,
                authorize_interaction=authorize_interaction,
            ),
            prepare_command=prepare_command,
            authorize_autocomplete=authorize_autocomplete,
            authorize_interaction=authorize_interaction,
        ),
    )


def compose_publication_delivery_worker(
    database_runtime: DatabaseRuntime,
    *,
    client: UmaSt2DiscordClient,
    settings: RuntimeSettings,
) -> MatchOddsPublicationRuntimeWorker:
    """Wire supported stored publications to the concrete Discord sender."""

    delivery_worker = PublicationDeliveryWorker(
        commands=compose_publication_delivery(database_runtime),
        sender=DiscordPublicationSenderClient(client),
        config=PublicationDeliveryWorkerConfig(
            max_batch_size=settings.win5_delivery_batch_size,
            max_attempts=settings.win5_delivery_max_attempts,
            retry_delay=settings.delivery_retry_delay,
            pending_timeout=settings.delivery_pending_timeout,
        ),
    )
    provisioning_worker = DiscordPublicationChannelProvisioningWorker(
        queries=compose_discord_publication_channel_provisioning_queries(database_runtime),
        commands=compose_discord_publication_channel_provisioning_commands(database_runtime),
        client=client,
        guild_id=str(settings.discord_guild_id),
        delivery_worker=delivery_worker,
        retry_delay=settings.delivery_retry_delay,
    )
    return MatchOddsPublicationRuntimeWorker(
        commands=compose_match_odds_publication(database_runtime),
        guild_id=str(settings.discord_guild_id),
        delivery_worker=provisioning_worker,
    )


@dataclass(slots=True)
class ComposedDiscordRuntime:
    """Own the concrete Discord client and runtime-scoped database resources."""

    client: UmaSt2DiscordClient
    database_runtime: DatabaseRuntime
    _disposed: bool = field(init=False, default=False)

    def run(self, token: str) -> None:
        """Run the blocking Discord client and surface terminal preflight failure."""

        self.client.run(token, log_handler=None)
        if self.client.startup_error is not None:
            raise RuntimeError("Discord runtime preflight failed.") from self.client.startup_error

    def dispose(self) -> None:
        """Release the runtime-scoped Engine/Pool exactly once."""

        if self._disposed:
            return
        self._disposed = True
        self.database_runtime.dispose()


def compose_discord_runtime(settings: RuntimeSettings) -> ComposedDiscordRuntime:
    """Build the executable guild-scoped V2 Discord runtime."""

    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        guild_settings_queries = compose_discord_guild_settings_queries(database_runtime)
        current_guild_settings = guild_settings_queries.get_guild_settings(guild_id=str(settings.discord_guild_id))
        validate_discord_runtime_settings(
            current_guild_settings,
            configured_guild_id=settings.discord_guild_id,
        )
        command_gate = DiscordCommandGate(
            settings_queries=guild_settings_queries,
            configured_guild_id=settings.discord_guild_id,
        )
        settings_command = create_settings_command(
            SettingsDiscordAdapter(
                queries=compose_discord_guild_settings_update_queries(database_runtime),
                commands=compose_discord_guild_settings_update_commands(database_runtime),
                prepare_command=command_gate.prepare_command,
                authorize_interaction=command_gate.authorize_interaction,
            )
        )
        win5_command_group = compose_win5_command_group(
            database_runtime,
            prepare_command=command_gate.prepare_command,
            authorize_autocomplete=command_gate.authorize_autocomplete,
            authorize_interaction=command_gate.authorize_interaction,
        )
        match_command_group = compose_match_command_group(
            database_runtime,
            prepare_command=command_gate.prepare_command,
            authorize_autocomplete=command_gate.authorize_autocomplete,
            authorize_interaction=command_gate.authorize_interaction,
        )
        export_command_group = compose_export_command_group(
            database_runtime,
            prepare_command=command_gate.prepare_command,
            authorize_autocomplete=command_gate.authorize_autocomplete,
        )
        account_command_group = compose_account_command_group(
            database_runtime,
            prepare_command=command_gate.prepare_command,
            authorize_interaction=command_gate.authorize_interaction,
        )
        staff_command_group = compose_staff_command_group(
            database_runtime,
            prepare_command=command_gate.prepare_command,
            authorize_autocomplete=command_gate.authorize_autocomplete,
            authorize_interaction=command_gate.authorize_interaction,
        )
        client = UmaSt2DiscordClient(
            configured_guild_id=settings.discord_guild_id,
            command_groups=(
                account_command_group,
                staff_command_group,
                settings_command,
                win5_command_group,
                match_command_group,
                export_command_group,
            ),
        )
        worker = compose_publication_delivery_worker(
            database_runtime,
            client=client,
            settings=settings,
        )
        scheduler = PublicationDeliveryScheduler(
            worker=worker,
            poll_interval=settings.delivery_poll_interval,
        )
        client.bind_runtime(
            preflight=DiscordRuntimePreflight(
                client=client,
                settings_queries=guild_settings_queries,
                configured_guild_id=settings.discord_guild_id,
            ),
            scheduler=scheduler,
        )
        return ComposedDiscordRuntime(
            client=client,
            database_runtime=database_runtime,
        )
    except BaseException:
        database_runtime.dispose()
        raise
