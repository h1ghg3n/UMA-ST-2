from collections.abc import Iterable


def has_role(role_ids: Iterable[int], required_role_id: int) -> bool:
    return required_role_id != 0 and required_role_id in set(role_ids)


def has_any_role(role_ids: Iterable[int], required_role_ids: Iterable[int]) -> bool:
    user_role_ids = set(role_ids)
    return any(required_role_id != 0 and required_role_id in user_role_ids for required_role_id in required_role_ids)


def can_operate(
    role_ids: Iterable[int],
    operator_role_id: int,
    bot_manager_role_id: int = 0,
    owner_role_id: int = 0,
) -> bool:
    return has_any_role(role_ids, (operator_role_id, bot_manager_role_id, owner_role_id))


def is_system_owner(role_ids: Iterable[int], owner_role_id: int) -> bool:
    return has_role(role_ids, owner_role_id)


def can_manage_bot(role_ids: Iterable[int], bot_manager_role_id: int, owner_role_id: int = 0) -> bool:
    return has_any_role(role_ids, (bot_manager_role_id, owner_role_id))
