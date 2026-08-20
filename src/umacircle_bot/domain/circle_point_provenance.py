"""Stable identifiers for canonical Circle Point transaction provenance."""

ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE = "account_registration_approval"
ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE = "initial_grant"
ACCOUNT_REGISTRATION_INITIAL_GRANT_IDEMPOTENCY_PREFIX = "account-registration-request-initial-grant"
STAFF_SOURCE_GAME_ACCOUNT_CLAIM_TRANSACTION_SOURCE = "staff_source_game_account_claim"
STAFF_SOURCE_GAME_ACCOUNT_CLAIM_INITIAL_GRANT_IDEMPOTENCY_PREFIX = "staff-source-game-account-claim-initial-grant"


def account_registration_initial_grant_idempotency_key(request_id: int) -> str:
    if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id <= 0:
        raise ValueError("account registration request ID must be a positive integer")
    return f"{ACCOUNT_REGISTRATION_INITIAL_GRANT_IDEMPOTENCY_PREFIX}:{request_id}"


def staff_source_game_account_claim_initial_grant_idempotency_key(game_account_id: int) -> str:
    if not isinstance(game_account_id, int) or isinstance(game_account_id, bool) or game_account_id <= 0:
        raise ValueError("source GameAccount ID must be a positive integer")
    return f"{STAFF_SOURCE_GAME_ACCOUNT_CLAIM_INITIAL_GRANT_IDEMPOTENCY_PREFIX}:{game_account_id}"
