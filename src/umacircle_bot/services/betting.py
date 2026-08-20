from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Race,
    RaceEntry,
    RaceResult,
)
from umacircle_bot.domain.betting import (
    MAX_POINT_AMOUNT,
    MatchPayout,
    calculate_match_payout_amount,
    calculate_match_payout_amount_from_effective_rate,
    is_match_bet_hit,
    normalize_match_bet_numbers,
    normalize_match_bet_type,
    validate_circle_point_balance,
    validate_positive_point_amount,
)
from umacircle_bot.domain.errors import (
    AccountNotFoundError,
    BetJudgementError,
    BetSettlementError,
    BettingRuleError,
    InsufficientCirclePointsError,
)
from umacircle_bot.domain.races import (
    MatchRaceSnapshot,
    MatchRaceStatus,
    evaluate_new_bet_eligibility,
    normalize_match_race_status,
)
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import BetJudgementDTO, CirclePointTransactionDTO, MatchBetDTO
from umacircle_bot.services.persona_wallets import (
    lock_persona_wallets,
)

SETTLEMENT_TRANSACTION_TYPE = "settlement_reward"
ROLLBACK_TRANSACTION_TYPE = "settlement_rollback"


def place_match_bet(
    session: Session,
    *,
    game_account_id: int,
    race_id: int,
    bet_type: str,
    numbers: Sequence[int],
    amount: int,
    event_id: int | None = None,
    placed_at: datetime | None = None,
    bet_close_minutes: int = 0,
    minimum_amount: int = 1,
    maximum_amount: int = MAX_POINT_AMOUNT,
    idempotency_key: str | None = None,
    expected_persona_id: str | None = None,
    actor_discord_user_id: str | None = None,
) -> MatchBetDTO:
    game_account_id = _normalize_positive_id(game_account_id, field_name="game account ID")
    race_id = _normalize_positive_id(race_id, field_name="race ID")
    if event_id is not None:
        event_id = _normalize_positive_id(event_id, field_name="event ID")
    validate_positive_point_amount(amount, field_name="bet amount")
    normalized_type = normalize_match_bet_type(bet_type)
    normalized_numbers = normalize_match_bet_numbers(normalized_type.value, numbers)
    normalized_idempotency_key = _normalize_bet_idempotency_key(idempotency_key)
    normalized_expected_persona_id = _normalize_optional_persona_id(expected_persona_id)
    normalized_actor_discord_user_id = (
        _normalize_discord_user_id(actor_discord_user_id) if actor_discord_user_id is not None else None
    )
    bet_persona_id: str | None = None

    try:
        with session.begin_nested():
            race = session.scalar(
                select(Race)
                .where(Race.id == race_id)
                .order_by(Race.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if race is None:
                raise BettingRuleError("race not found")

            game_account, point_account = _lock_match_bet_authority_and_wallet(
                session,
                game_account_id=game_account_id,
                actor_discord_user_id=normalized_actor_discord_user_id,
            )
            bet_persona_id = point_account.persona_id
            if normalized_expected_persona_id is not None and bet_persona_id != normalized_expected_persona_id:
                raise BettingRuleError("selected game account Persona ownership changed")

            if normalized_idempotency_key is not None:
                retry_bet = _load_idempotent_match_bet(
                    session,
                    idempotency_key=normalized_idempotency_key,
                    persona_id=bet_persona_id,
                    game_account_id=game_account_id,
                    race_id=race_id,
                    event_id=event_id,
                    bet_type=normalized_type.value,
                    numbers=normalized_numbers,
                    amount=amount,
                    lock_row=False,
                )
                if retry_bet is not None:
                    return _bet_dto(retry_bet)

            if race.external_source is not None:
                raise BettingRuleError("imported historical races are read-only")

            _normalize_utc_datetime(
                placed_at or datetime.now(UTC),
                field_name="bet placement time",
            )
            _validate_bet_amount_limits(minimum_amount, maximum_amount)
            if not minimum_amount <= amount <= maximum_amount:
                raise BettingRuleError(f"bet amount must be between {minimum_amount} and {maximum_amount}")
            if (
                not isinstance(bet_close_minutes, int)
                or isinstance(bet_close_minutes, bool)
                or not 0 <= bet_close_minutes <= 10_080
            ):
                raise BettingRuleError("bet close minutes must be an integer between 0 and 10080")

            if event_id is not None and event_id != race.event_id:
                raise BettingRuleError("event does not match the selected race")

            entry_count = session.scalar(
                select(func.count(RaceEntry.id)).where(
                    RaceEntry.race_id == race.id,
                    RaceEntry.entry_kind == "room_match",
                )
            )
            has_results = (
                session.scalar(select(RaceResult.id).where(RaceResult.race_id == race.id).limit(1)) is not None
            )
            if race.race_kind != "room_match":
                raise BettingRuleError("race is not a room match")
            decision = evaluate_new_bet_eligibility(
                MatchRaceSnapshot(
                    race_kind=race.race_kind,
                    status=normalize_match_race_status(race.status),
                    starts_at=database_datetime_as_utc(race.starts_at) if race.starts_at is not None else None,
                    has_condition=False,
                    entry_count=entry_count or 0,
                    has_results=has_results,
                )
            )
            if not decision.allowed:
                raise BettingRuleError(_eligibility_error_message(decision.code))

            if point_account.balance < amount:
                raise InsufficientCirclePointsError("room point balance is insufficient")
            available_numbers = set(
                session.scalars(
                    select(RaceEntry.entry_number).where(
                        RaceEntry.race_id == race.id,
                        RaceEntry.entry_kind == "room_match",
                        RaceEntry.entry_number.in_(normalized_numbers),
                    )
                )
            )
            if set(normalized_numbers) - available_numbers:
                raise BettingRuleError("bet numbers are not registered for the selected race")

            # The locked Persona wallet is the duplicate-check serialization
            # root. A second FOR UPDATE range scan here creates unnecessary
            # InnoDB gap-lock cycles between unrelated Personas/Races.
            active_bets = session.scalars(
                select(Bet)
                .where(
                    Bet.persona_id == bet_persona_id,
                    Bet.race_id == race.id,
                    Bet.betting_mode == "room_match",
                    Bet.bet_type == normalized_type.value,
                    Bet.status == "active",
                )
                .order_by(Bet.id)
            )
            if any(tuple(existing.numbers) == tuple(normalized_numbers) for existing in active_bets):
                raise BettingRuleError("matching active room-match bet already exists")

            bet = Bet(
                event_id=race.event_id,
                race_id=race.id,
                persona_id=bet_persona_id,
                game_account_id=game_account.id,
                betting_mode="room_match",
                bet_type=normalized_type.value,
                numbers=normalized_numbers,
                amount=amount,
                betting_window_version=race.betting_window_version,
                status="active",
            )
            session.add(bet)
            session.flush()

            point_account.balance -= amount
            session.add(
                CirclePointTransaction(
                    persona_id=bet_persona_id,
                    game_account_id=game_account.id,
                    type="bet_stake",
                    amount=-amount,
                    reason=f"room_match_bet:{bet.id}",
                    source="room_match_bet",
                    related_bet_id=bet.id,
                    idempotency_key=normalized_idempotency_key,
                )
            )
            session.flush()
    except IntegrityError as exc:
        if normalized_idempotency_key is None or bet_persona_id is None:
            raise
        with session.begin_nested():
            retry_bet = _load_idempotent_match_bet(
                session,
                idempotency_key=normalized_idempotency_key,
                persona_id=bet_persona_id,
                game_account_id=game_account_id,
                race_id=race_id,
                event_id=event_id,
                bet_type=normalized_type.value,
                numbers=normalized_numbers,
                amount=amount,
                lock_row=True,
            )
            if retry_bet is None:
                raise exc
            return _bet_dto(retry_bet)

    return _bet_dto(bet)


def _lock_match_bet_authority_and_wallet(
    session: Session,
    *,
    game_account_id: int,
    actor_discord_user_id: str | None,
) -> tuple[GameAccount, CirclePointAccount]:
    """Lock current actor authority, provenance account, and wallet in that order."""

    actor: DiscordAccount | None = None
    if actor_discord_user_id is not None:
        actor = session.scalar(
            select(DiscordAccount)
            .where(DiscordAccount.discord_user_id == actor_discord_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if actor is None:
            raise AccountNotFoundError("discord account not found")

    game_account = session.scalar(
        select(GameAccount)
        .where(GameAccount.id == game_account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if game_account is None:
        raise AccountNotFoundError("game account not found")
    if game_account.persona_id is None:
        raise AccountNotFoundError("game account has no Persona owner")
    if actor is not None and actor.persona_id != game_account.persona_id:
        raise BettingRuleError("Discord account no longer owns the selected game account")

    point_account = session.scalar(
        select(CirclePointAccount)
        .where(CirclePointAccount.persona_id == game_account.persona_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if point_account is None:
        raise AccountNotFoundError("room point account not found")
    return game_account, point_account


def _eligibility_error_message(code: str | None) -> str:
    return {
        "wrong_race_kind": "selected race is not a room match",
        "betting_not_open": "race is not open for betting",
        "results_exist": "race results already exist",
        "no_entries": "bet numbers are not registered for the selected race",
        "missing_start_time": "race betting deadline is not configured",
        "cutoff_reached": "race betting is closed",
    }.get(code, "race is not eligible for betting")


def _bet_dto(bet: Bet) -> MatchBetDTO:
    return MatchBetDTO(
        id=bet.id,
        event_id=bet.event_id,
        race_id=bet.race_id,
        persona_id=bet.persona_id,
        game_account_id=bet.game_account_id,
        betting_mode=bet.betting_mode,
        bet_type=bet.bet_type,
        numbers=tuple(bet.numbers),
        amount=bet.amount,
        betting_window_version=bet.betting_window_version,
        status=bet.status,
    )


def _load_idempotent_match_bet(
    session: Session,
    *,
    idempotency_key: str,
    persona_id: str,
    game_account_id: int,
    race_id: int,
    event_id: int | None,
    bet_type: str,
    numbers: Sequence[int],
    amount: int,
    lock_row: bool,
) -> Bet | None:
    transaction_query = select(CirclePointTransaction).where(CirclePointTransaction.idempotency_key == idempotency_key)
    if lock_row:
        transaction_query = transaction_query.with_for_update()
    transaction = session.scalar(transaction_query)
    if transaction is None:
        return None
    if transaction.related_bet_id is None:
        raise BettingRuleError("bet idempotency key is already used")
    bet = session.scalar(select(Bet).where(Bet.id == transaction.related_bet_id).with_for_update())
    if bet is None:
        raise BettingRuleError("stored idempotent bet is missing")
    if (
        transaction.type != "bet_stake"
        or transaction.source != "room_match_bet"
        or transaction.persona_id != persona_id
        or transaction.game_account_id != game_account_id
        or transaction.amount != -amount
        or bet.persona_id != persona_id
        or bet.game_account_id != game_account_id
        or bet.race_id != race_id
        or (event_id is not None and bet.event_id != event_id)
        or bet.betting_mode != "room_match"
        or bet.bet_type != bet_type
        or tuple(bet.numbers) != tuple(numbers)
        or bet.amount != amount
    ):
        raise BettingRuleError("bet idempotency key payload does not match")
    return bet


def judge_match_bets(
    session: Session,
    *,
    race_id: int,
    judged_by_discord_user_id: str,
) -> tuple[BetJudgementDTO, ...]:
    race_id = _normalize_positive_id(race_id, field_name="race ID")
    actor_id = _normalize_discord_user_id(judged_by_discord_user_id)

    with session.begin_nested():
        race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
        if race is None:
            raise BettingRuleError("race not found")
        _validate_match_result_race(race)

        bets = list(
            session.scalars(
                select(Bet)
                .where(
                    Bet.race_id == race_id,
                    Bet.betting_mode == "room_match",
                    Bet.status.in_(("active", "settled")),
                )
                .order_by(Bet.id)
                .with_for_update()
            )
        )
        if not bets:
            return ()

        finishers_by_rank = _load_confirmed_finishers(session, race_id)
        existing_by_bet_id = _load_judgements_by_bet_id(session, [bet.id for bet in bets])
        judged_at = datetime.now(UTC)
        judgements: list[BetJudgement] = []

        for bet in bets:
            is_hit = is_match_bet_hit(bet.bet_type, bet.numbers, finishers_by_rank)
            judgement_status = "hit" if is_hit else "miss"
            existing = existing_by_bet_id.get(bet.id)
            if existing is not None:
                _verify_existing_judgement(existing, bet, judgement_status, is_hit)
                judgements.append(existing)
                continue
            if bet.status != "active":
                raise BetJudgementError("settled bet is missing its judgement record")

            judgement = BetJudgement(
                bet_id=bet.id,
                race_id=race.id,
                judgement_status=judgement_status,
                is_hit=is_hit,
                stake_amount=bet.amount,
                payout_amount=0,
                point_delta=0,
                judged_at=judged_at,
                judged_by_discord_user_id=actor_id,
            )
            session.add(judgement)
            judgements.append(judgement)

        session.flush()

    bets_by_id = {bet.id: bet for bet in bets}
    return tuple(_judgement_dto(judgement, bets_by_id[judgement.bet_id]) for judgement in judgements)


def settle_match_bets(
    session: Session,
    *,
    race_id: int,
    payouts_by_bet_id: Mapping[int, MatchPayout],
    settled_by_discord_user_id: str,
    payout_rates_are_effective: bool = False,
    locked_wallets_by_persona: Mapping[str, CirclePointAccount] | None = None,
) -> tuple[CirclePointTransactionDTO, ...]:
    race_id = _normalize_positive_id(race_id, field_name="race ID")
    actor_id = _normalize_discord_user_id(settled_by_discord_user_id)
    payouts = _normalize_payouts(payouts_by_bet_id)
    if not isinstance(payout_rates_are_effective, bool):
        raise BetSettlementError("payout rate snapshot flag must be boolean")

    with session.begin_nested():
        race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
        if race is None:
            raise BettingRuleError("race not found")
        _validate_match_result_race(race)
        entry_count = session.scalar(
            select(func.count(RaceEntry.id)).where(
                RaceEntry.race_id == race_id,
                RaceEntry.entry_kind == "room_match",
            )
        )

        bets = list(
            session.scalars(
                select(Bet)
                .where(Bet.race_id == race_id, Bet.betting_mode == "room_match")
                .order_by(Bet.id)
                .with_for_update()
            )
        )
        bets_by_id = {bet.id: bet for bet in bets}
        judgements = list(
            session.scalars(
                select(BetJudgement)
                .where(BetJudgement.race_id == race_id)
                .order_by(BetJudgement.bet_id)
                .with_for_update()
            )
        )
        if not judgements:
            if payouts:
                raise BetSettlementError("payouts were supplied for a race without judgements")
            if any(bet.status == "active" for bet in bets):
                raise BetSettlementError("all active bets must be judged before settlement")
            return ()

        _ensure_one_judgement_per_bet(judgements)
        judged_bet_ids = {judgement.bet_id for judgement in judgements}
        if any(bet.status == "active" and bet.id not in judged_bet_ids for bet in bets):
            raise BetSettlementError("all active bets must be judged before settlement")
        hit_bet_ids = {judgement.bet_id for judgement in judgements if judgement.judgement_status == "hit"}
        if unexpected_ids := set(payouts) - hit_bet_ids:
            raise BetSettlementError(f"payout supplied for non-hit or unknown bet: {min(unexpected_ids)}")

        transaction_rows = list(
            session.scalars(
                select(CirclePointTransaction)
                .where(
                    CirclePointTransaction.related_bet_id.in_([judgement.bet_id for judgement in judgements]),
                    CirclePointTransaction.type.in_((SETTLEMENT_TRANSACTION_TYPE, ROLLBACK_TRANSACTION_TYPE)),
                )
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        transactions_by_bet_id: dict[int, list[CirclePointTransaction]] = {}
        for transaction in transaction_rows:
            transactions_by_bet_id.setdefault(transaction.related_bet_id, []).append(transaction)

        existing_transactions: list[CirclePointTransaction] = []
        pending: list[tuple[Bet, BetJudgement, MatchPayout]] = []
        for judgement in judgements:
            bet = bets_by_id.get(judgement.bet_id)
            if bet is None:
                raise BetSettlementError("judgement references a missing room-match bet")
            _verify_judgement_identity(judgement, bet)
            related_transactions = transactions_by_bet_id.get(bet.id, [])
            settlements = [row for row in related_transactions if row.type == SETTLEMENT_TRANSACTION_TYPE]
            rollbacks = [row for row in related_transactions if row.type == ROLLBACK_TRANSACTION_TYPE]

            if bet.status == "settled":
                if len(settlements) != 1 or rollbacks:
                    raise BetSettlementError("settled bet has inconsistent transaction history")
                _verify_settlement_transaction(settlements[0], judgement, bet)
                _verify_retry_payout(judgement, payouts.get(bet.id))
                existing_transactions.append(settlements[0])
                continue
            if bet.status != "active":
                raise BetSettlementError("only active judged bets can be settled")
            if settlements or rollbacks:
                raise BetSettlementError("rolled-back settlement cannot be reapplied without a correction workflow")
            if judgement.judgement_status == "hit":
                payout = payouts.get(bet.id)
                if payout is None:
                    raise BetSettlementError("every unsettled hit requires an operator-confirmed payout")
            elif judgement.judgement_status == "miss":
                payout = MatchPayout(payout_amount=0)
            else:
                raise BetSettlementError("judgement is not ready for settlement")
            if payout.payout_rate is not None:
                if payout_rates_are_effective:
                    expected_payout_amount = calculate_match_payout_amount_from_effective_rate(
                        bet.amount,
                        payout.payout_rate,
                    )
                else:
                    if not entry_count:
                        raise BetSettlementError("room-match settlement requires the final entry snapshot")
                    expected_payout_amount = calculate_match_payout_amount(
                        bet.amount,
                        payout.payout_rate,
                        entry_count,
                    )
                if payout.payout_amount != expected_payout_amount:
                    raise BetSettlementError("payout amount does not match the final-entry multiplier")
            pending.append((bet, judgement, payout))

        pending_persona_ids = {bet.persona_id for bet, _, _ in pending}
        if locked_wallets_by_persona is None:
            wallets_by_persona = lock_persona_wallets(
                session,
                persona_ids=pending_persona_ids,
            )
        else:
            wallets_by_persona = dict(locked_wallets_by_persona)
            if not pending_persona_ids.issubset(wallets_by_persona) or any(
                persona_id != wallet.persona_id for persona_id, wallet in wallets_by_persona.items()
            ):
                raise BetSettlementError("prelocked Circle Point wallets do not cover the Bet settlement")

        new_transactions: list[CirclePointTransaction] = []
        for bet, judgement, payout in pending:
            point_account = wallets_by_persona[bet.persona_id]
            if point_account.balance < 0:
                raise BetSettlementError("room point balance would exceed the supported range")
            try:
                validate_circle_point_balance(point_account.balance + payout.payout_amount)
            except BettingRuleError as exc:
                raise BetSettlementError("room point balance would exceed the supported range") from exc

            point_account.balance += payout.payout_amount
            judgement.payout_rate = payout.payout_rate
            judgement.payout_amount = payout.payout_amount
            judgement.point_delta = payout.payout_amount
            bet.status = "settled"
            transaction = CirclePointTransaction(
                persona_id=bet.persona_id,
                game_account_id=bet.game_account_id,
                type=SETTLEMENT_TRANSACTION_TYPE,
                amount=payout.payout_amount,
                reason=f"room_match_settlement:{judgement.id}",
                source="room_match_settlement",
                related_bet_id=bet.id,
                created_by_discord_user_id=actor_id,
            )
            session.add(transaction)
            new_transactions.append(transaction)

        session.flush()

    return tuple(_transaction_dto(transaction) for transaction in existing_transactions + new_transactions)


def rollback_match_settlement(
    session: Session,
    *,
    bet_id: int,
    rolled_back_by_discord_user_id: str,
    reason: str,
) -> CirclePointTransactionDTO:
    bet_id = _normalize_positive_id(bet_id, field_name="bet ID")
    _normalize_discord_user_id(rolled_back_by_discord_user_id)
    _normalize_rollback_reason(reason)

    with session.begin_nested():
        race_id = session.scalar(select(Bet.race_id).where(Bet.id == bet_id))
        if race_id is None:
            raise BetSettlementError("room-match bet not found")
        race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
        if race is None:
            raise BetSettlementError("room-match race not found")
        _validate_match_result_race(race)
        bet = session.scalar(select(Bet).where(Bet.id == bet_id, Bet.race_id == race.id).with_for_update())
        if bet is None or bet.betting_mode != "room_match":
            raise BetSettlementError("room-match bet not found")
        raise BetSettlementError("per-Bet settlement rollback is unsupported; use race-level settlement rollback")


def _judgement_dto(judgement: BetJudgement, bet: Bet) -> BetJudgementDTO:
    return BetJudgementDTO(
        judgement.id,
        judgement.bet_id,
        judgement.race_id,
        bet.game_account_id,
        bet.bet_type,
        tuple(bet.numbers),
        judgement.stake_amount,
        judgement.judgement_status,
        judgement.is_hit,
        judgement.payout_amount,
        judgement.point_delta,
        judgement.payout_rate,
        database_datetime_as_utc(judgement.judged_at) if judgement.judged_at is not None else None,
        judgement.judged_by_discord_user_id,
    )


def _transaction_dto(transaction: CirclePointTransaction) -> CirclePointTransactionDTO:
    return CirclePointTransactionDTO(
        id=transaction.id,
        persona_id=transaction.persona_id,
        game_account_id=transaction.game_account_id,
        type=transaction.type,
        amount=transaction.amount,
        reason=transaction.reason,
        source=transaction.source,
        related_bet_id=transaction.related_bet_id,
        related_race_result_id=transaction.related_race_result_id,
        created_by_discord_user_id=transaction.created_by_discord_user_id,
        created_at=database_datetime_as_utc(transaction.created_at),
    )


def _load_confirmed_finishers(session: Session, race_id: int) -> dict[int, int]:
    room_match_entry_numbers = set(
        session.scalars(
            select(RaceEntry.entry_number)
            .where(
                RaceEntry.race_id == race_id,
                RaceEntry.entry_kind == "room_match",
            )
            .order_by(RaceEntry.entry_number)
            .with_for_update()
        )
    )
    if not room_match_entry_numbers:
        raise BetJudgementError("confirmed room-match entry snapshot is required")
    results = list(
        session.scalars(
            select(RaceResult).where(RaceResult.race_id == race_id).order_by(RaceResult.rank).with_for_update()
        )
    )
    if not results:
        raise BetJudgementError("confirmed race results are required")
    if any(result.entry_number not in room_match_entry_numbers for result in results):
        raise BetJudgementError("race results do not match the room-match entry snapshot")

    finishers_by_rank: dict[int, int] = {}
    for result in results:
        if result.is_betting_excluded or result.is_result_void:
            continue
        if result.rank <= 0 or result.entry_number <= 0:
            raise BetJudgementError("race results contain invalid rank or entry numbers")
        if result.rank in finishers_by_rank:
            raise BetJudgementError("race results contain duplicate ranks")
        finishers_by_rank[result.rank] = result.entry_number
    return finishers_by_rank


def _validate_match_result_race(race: Race) -> None:
    if race.race_kind != "room_match":
        raise BettingRuleError("selected race is not a room match")
    if race.external_source is not None:
        raise BettingRuleError("imported historical races are read-only")
    status = normalize_match_race_status(race.status)
    if status not in {
        MatchRaceStatus.RESULT_CONFIRMED,
        MatchRaceStatus.SETTLED,
    }:
        raise BettingRuleError("confirmed room-match results are required")


def _load_judgements_by_bet_id(session: Session, bet_ids: list[int]) -> dict[int, BetJudgement]:
    judgements = list(session.scalars(select(BetJudgement).where(BetJudgement.bet_id.in_(bet_ids)).with_for_update()))
    _ensure_one_judgement_per_bet(judgements)
    return {judgement.bet_id: judgement for judgement in judgements}


def _ensure_one_judgement_per_bet(judgements: Sequence[BetJudgement]) -> None:
    bet_ids = [judgement.bet_id for judgement in judgements]
    if len(bet_ids) != len(set(bet_ids)):
        raise BetJudgementError("bet has multiple judgement records")


def _verify_existing_judgement(
    judgement: BetJudgement,
    bet: Bet,
    expected_status: str,
    expected_is_hit: bool,
) -> None:
    if judgement.judgement_status != expected_status or judgement.is_hit is not expected_is_hit:
        raise BetJudgementError("stored judgement no longer matches the confirmed result")
    _verify_judgement_identity(judgement, bet)


def _verify_judgement_identity(judgement: BetJudgement, bet: Bet) -> None:
    if judgement.race_id != bet.race_id or judgement.stake_amount != bet.amount:
        raise BetJudgementError("judgement does not match its room-match bet")
    if judgement.judgement_status == "hit" and not judgement.is_hit:
        raise BetJudgementError("hit judgement has an inconsistent hit flag")
    if judgement.judgement_status == "miss" and judgement.is_hit:
        raise BetJudgementError("miss judgement has an inconsistent hit flag")


def _normalize_payouts(payouts: Mapping[int, MatchPayout]) -> dict[int, MatchPayout]:
    if not isinstance(payouts, Mapping):
        raise BetSettlementError("payouts must be mapped by bet ID")
    normalized: dict[int, MatchPayout] = {}
    for bet_id, payout in payouts.items():
        if not isinstance(bet_id, int) or isinstance(bet_id, bool) or bet_id <= 0:
            raise BetSettlementError("payout bet IDs must be positive integers")
        if not isinstance(payout, MatchPayout):
            raise BetSettlementError("payout values must be MatchPayout instances")
        normalized[bet_id] = payout
    return normalized


def _verify_retry_payout(judgement: BetJudgement, payout: MatchPayout | None) -> None:
    if payout is None:
        return
    if judgement.payout_amount != payout.payout_amount or judgement.payout_rate != payout.payout_rate:
        raise BetSettlementError("retry payout does not match the stored settlement")


def _verify_settlement_transaction(
    transaction: CirclePointTransaction,
    judgement: BetJudgement,
    bet: Bet,
) -> None:
    if (
        transaction.persona_id != bet.persona_id
        or transaction.game_account_id != bet.game_account_id
        or transaction.amount != judgement.payout_amount
        or transaction.amount != judgement.point_delta
        or not 0 <= transaction.amount <= MAX_POINT_AMOUNT
    ):
        raise BetSettlementError("settlement transaction does not match its judgement")


def _normalize_positive_id(value: int, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BettingRuleError(f"{field_name} must be a positive integer")
    return value


def _normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BettingRuleError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_bet_amount_limits(minimum_amount: int, maximum_amount: int) -> None:
    validate_positive_point_amount(minimum_amount, field_name="minimum bet amount")
    validate_positive_point_amount(maximum_amount, field_name="maximum bet amount")
    if minimum_amount > maximum_amount:
        raise BettingRuleError("minimum bet amount must not exceed maximum bet amount")


def _normalize_bet_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BettingRuleError("bet idempotency key must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
        raise BettingRuleError("bet idempotency key must contain 1 to 128 printable characters")
    return normalized


def _normalize_optional_persona_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BettingRuleError("expected Persona ID must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 36 or any(ord(character) < 32 for character in normalized):
        raise BettingRuleError("expected Persona ID must contain 1 to 36 printable characters")
    return normalized


def _normalize_discord_user_id(value: str) -> str:
    if not isinstance(value, str):
        raise BettingRuleError("Discord user ID must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 32 or not normalized.isascii() or not normalized.isdigit():
        raise BettingRuleError("Discord user ID must contain 1 to 32 ASCII digits")
    return normalized


def _normalize_rollback_reason(value: str) -> str:
    if not isinstance(value, str):
        raise BetSettlementError("rollback reason must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 180 or any(ord(character) < 32 for character in normalized):
        raise BetSettlementError("rollback reason must contain 1 to 180 printable characters")
    return normalized
