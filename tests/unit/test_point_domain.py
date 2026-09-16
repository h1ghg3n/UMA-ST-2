from __future__ import annotations

import ast
from pathlib import Path

import pytest

from uma_st2.domain.point import (
    CirclePoint,
    PointAmountError,
    calculate_next_circle_point_balance,
)


def test_apply_positive_signed_amount() -> None:
    assert calculate_next_circle_point_balance(120, 15) == 135


def test_apply_negative_signed_amount() -> None:
    assert calculate_next_circle_point_balance(120, -15) == 105


def test_exact_arithmetic_preservation_with_signed_deltas() -> None:
    current = 10_000
    next_balance = calculate_next_circle_point_balance(current, 17)
    restored = calculate_next_circle_point_balance(next_balance, -17)

    assert restored == current


def test_circle_point_owner_and_current_balance_semantics() -> None:
    wallet = CirclePoint(persona_id="persona-1", balance=12)
    rewarded = wallet.apply_delta(3)

    assert rewarded.persona_id == wallet.persona_id
    assert rewarded.balance == 15
    assert wallet is not rewarded


def test_point_amount_type_is_required() -> None:
    with pytest.raises(PointAmountError):
        calculate_next_circle_point_balance("1", 10)

    with pytest.raises(PointAmountError):
        calculate_next_circle_point_balance(1, "10")


def test_point_domain_has_no_cross_domain_or_infra_imports() -> None:
    point_module_path = Path("src/uma_st2/domain/point")
    source_paths = [
        point_module_path / "__init__.py",
        point_module_path / "kernel.py",
        point_module_path / "models.py",
        point_module_path / "errors.py",
    ]

    imported_modules: set[str] = set()
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)

    forbidden = {
        "uma_st2.domain.identity",
        "uma_st2.domain.match",
        "uma_st2.domain.rating",
        "uma_st2.domain.betting",
        "uma_st2.domain.win5",
        "sqlalchemy",
        "discord",
        "adapter",
        "application",
        "infrastructure",
    }

    assert not any(module.startswith(forbidden_module) for module in imported_modules for forbidden_module in forbidden)
